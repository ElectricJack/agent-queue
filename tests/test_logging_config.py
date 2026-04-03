"""Tests for structured logging and correlation context."""

from __future__ import annotations

import json
import logging

import pytest

from src.logging_config import (
    CorrelationContext,
    HumanReadableFormatter,
    StructuredFormatter,
    get_correlation_context,
    setup_logging,
)


class TestCorrelationContext:
    """Tests for the CorrelationContext context manager."""

    def test_empty_context(self):
        ctx = get_correlation_context()
        assert ctx == {}

    def test_set_task_id(self):
        with CorrelationContext(task_id="swift-dawn"):
            ctx = get_correlation_context()
            assert ctx["task_id"] == "swift-dawn"
        # Restored after exit
        assert get_correlation_context() == {}

    def test_set_multiple_fields(self):
        with CorrelationContext(
            task_id="swift-dawn",
            project_id="acme",
            cycle_id="cycle-1",
            component="orchestrator",
        ):
            ctx = get_correlation_context()
            assert ctx == {
                "task_id": "swift-dawn",
                "project_id": "acme",
                "cycle_id": "cycle-1",
                "component": "orchestrator",
            }
        assert get_correlation_context() == {}

    def test_nested_context(self):
        with CorrelationContext(task_id="outer", project_id="proj"):
            with CorrelationContext(task_id="inner"):
                ctx = get_correlation_context()
                assert ctx["task_id"] == "inner"
                assert ctx["project_id"] == "proj"
            # Inner restored, outer still active
            ctx = get_correlation_context()
            assert ctx["task_id"] == "outer"
            assert ctx["project_id"] == "proj"

    def test_partial_override(self):
        with CorrelationContext(task_id="t1", project_id="p1"):
            with CorrelationContext(project_id="p2"):
                ctx = get_correlation_context()
                assert ctx["task_id"] == "t1"
                assert ctx["project_id"] == "p2"

    def test_hook_id_field(self):
        with CorrelationContext(hook_id="hook-123", project_id="proj"):
            ctx = get_correlation_context()
            assert ctx["hook_id"] == "hook-123"
            assert ctx["project_id"] == "proj"
        assert "hook_id" not in get_correlation_context()

    def test_agent_id_field(self):
        with CorrelationContext(agent_id="agent-1", task_id="task-1"):
            ctx = get_correlation_context()
            assert ctx["agent_id"] == "agent-1"
            assert ctx["task_id"] == "task-1"
        assert "agent_id" not in get_correlation_context()

    def test_command_field(self):
        with CorrelationContext(command="get_status", component="command_handler"):
            ctx = get_correlation_context()
            assert ctx["command"] == "get_status"
            assert ctx["component"] == "command_handler"
        assert "command" not in get_correlation_context()

    def test_all_fields_together(self):
        with CorrelationContext(
            task_id="t1",
            project_id="p1",
            cycle_id="c1",
            component="orch",
            hook_id="h1",
            agent_id="a1",
            command="cmd",
        ):
            ctx = get_correlation_context()
            assert len(ctx) == 7
            assert ctx["task_id"] == "t1"
            assert ctx["hook_id"] == "h1"
            assert ctx["agent_id"] == "a1"
            assert ctx["command"] == "cmd"

    def test_nested_hook_and_task_context(self):
        """Hook context nested inside task context preserves task fields."""
        with CorrelationContext(task_id="t1", project_id="p1"):
            with CorrelationContext(hook_id="h1", component="hooks"):
                ctx = get_correlation_context()
                assert ctx["task_id"] == "t1"
                assert ctx["project_id"] == "p1"
                assert ctx["hook_id"] == "h1"
                assert ctx["component"] == "hooks"
            # hook_id cleared, component restored
            ctx = get_correlation_context()
            assert "hook_id" not in ctx
            assert "component" not in ctx


class TestStructuredFormatter:
    """Tests for JSON-lines output formatter."""

    def _make_record(self, msg: str, level: int = logging.INFO, **extra) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.module",
            level=level,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_basic_json_output(self):
        fmt = StructuredFormatter()
        record = self._make_record("Hello world")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.module"
        assert parsed["message"] == "Hello world"
        assert "timestamp" in parsed

    def test_correlation_context_in_output(self):
        fmt = StructuredFormatter()
        with CorrelationContext(task_id="swift-dawn", project_id="acme"):
            record = self._make_record("Task started")
            output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["task_id"] == "swift-dawn"
        assert parsed["project_id"] == "acme"

    def test_no_correlation_when_not_set(self):
        fmt = StructuredFormatter()
        record = self._make_record("No context")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "task_id" not in parsed
        assert "project_id" not in parsed

    def test_include_source(self):
        fmt = StructuredFormatter(include_source=True)
        record = self._make_record("With source")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["filename"] == "test.py"
        assert parsed["lineno"] == 42

    def test_exclude_source_by_default(self):
        fmt = StructuredFormatter(include_source=False)
        record = self._make_record("No source")
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "filename" not in parsed
        assert "lineno" not in parsed

    def test_new_correlation_fields_in_output(self):
        fmt = StructuredFormatter()
        with CorrelationContext(hook_id="h1", agent_id="a1", command="deploy"):
            record = self._make_record("Hook running")
            output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["hook_id"] == "h1"
        assert parsed["agent_id"] == "a1"
        assert parsed["command"] == "deploy"

    def test_exception_info(self):
        fmt = StructuredFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            record = self._make_record("Error occurred")
            record.exc_info = sys.exc_info()
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError: test error" in parsed["exception"]


class TestHumanReadableFormatter:
    """Tests for human-readable text formatter."""

    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="test.module",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_basic_text_output(self):
        fmt = HumanReadableFormatter()
        record = self._make_record("Hello world")
        output = fmt.format(record)
        assert "INFO" in output
        assert "[test.module]" in output
        assert "Hello world" in output

    def test_correlation_in_text(self):
        fmt = HumanReadableFormatter()
        with CorrelationContext(task_id="swift-dawn"):
            record = self._make_record("Task started")
            output = fmt.format(record)
        assert "task_id=swift-dawn" in output


class TestSetupLogging:
    """Tests for the setup_logging function."""

    def test_setup_text_format(self):
        setup_logging(level="DEBUG", format="text")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) >= 1
        assert isinstance(root.handlers[-1].formatter, HumanReadableFormatter)

    def test_setup_json_format(self):
        setup_logging(level="WARNING", format="json")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert isinstance(root.handlers[-1].formatter, StructuredFormatter)

    def test_replaces_existing_handlers(self):
        # Add a dummy handler
        root = logging.getLogger()
        dummy = logging.StreamHandler()
        root.addHandler(dummy)
        handler_count = len(root.handlers)

        setup_logging(level="INFO", format="text")
        # Should have replaced all handlers with just one
        assert len(root.handlers) == 1
