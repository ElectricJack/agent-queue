"""Tests for structlog-powered logging and correlation context."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from src.logging_config import (
    CorrelationContext,
    get_correlation_context,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clear_contextvars():
    """Ensure each test starts with clean contextvars."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


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

    def test_run_id_via_extra_kwargs(self):
        # run_id isn't a named parameter but flows through **extra — used by
        # playbook runner call sites to tag logs with the playbook run id.
        with CorrelationContext(run_id="bb8e481e-7df", task_id="t-1"):
            ctx = get_correlation_context()
            assert ctx["run_id"] == "bb8e481e-7df"
            assert ctx["task_id"] == "t-1"
        assert "run_id" not in get_correlation_context()

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

    def test_extra_kwargs(self):
        """Arbitrary extra fields can be bound via **extra."""
        with CorrelationContext(plugin="my-plugin", platform="discord"):
            ctx = get_correlation_context()
            assert ctx["plugin"] == "my-plugin"
            assert ctx["platform"] == "discord"
        assert get_correlation_context() == {}


class TestSetupLogging:
    """Tests for the setup_logging function."""

    def test_setup_dev_format(self):
        setup_logging(level="DEBUG", format="dev")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) >= 1
        fmt = root.handlers[0].formatter
        assert isinstance(fmt, structlog.stdlib.ProcessorFormatter)

    def test_setup_json_format(self):
        setup_logging(level="WARNING", format="json")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert isinstance(root.handlers[0].formatter, structlog.stdlib.ProcessorFormatter)

    def test_setup_plain_format(self):
        setup_logging(level="INFO", format="plain")
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert isinstance(root.handlers[0].formatter, structlog.stdlib.ProcessorFormatter)

    def test_text_backward_compat(self):
        """'text' is accepted as alias for 'dev'."""
        setup_logging(level="INFO", format="text")
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        assert isinstance(root.handlers[0].formatter, structlog.stdlib.ProcessorFormatter)

    def test_replaces_existing_handlers(self):
        root = logging.getLogger()
        dummy = logging.StreamHandler()
        root.addHandler(dummy)

        setup_logging(level="INFO", format="dev")
        # Should have replaced all handlers with just one (+ discord rate guard)
        non_rate_guard = [
            h for h in root.handlers if not type(h).__name__ == "DiscordHTTPLogHandler"
        ]
        assert len(non_rate_guard) == 1

    def test_file_handler_created(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging(level="INFO", format="dev", log_file=log_file)

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if hasattr(h, "baseFilename")]
        assert len(file_handlers) == 1

    def test_no_file_handler_when_empty(self):
        setup_logging(level="INFO", format="dev", log_file="")
        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if hasattr(h, "baseFilename")]
        assert len(file_handlers) == 0


class TestStructlogOutput:
    """Tests that log output contains expected fields."""

    def test_json_output_contains_correlation(self, tmp_path):
        """JSON output includes correlation context fields."""
        log_file = str(tmp_path / "test.log")
        setup_logging(level="INFO", format="json", log_file=log_file)

        test_logger = logging.getLogger("test.json_output")
        with CorrelationContext(task_id="swift-dawn", project_id="acme"):
            test_logger.info("Task started")

        # Read from the JSONL file
        with open(log_file) as f:
            lines = [line.strip() for line in f if line.strip()]

        assert len(lines) >= 1
        parsed = json.loads(lines[-1])
        assert parsed["task_id"] == "swift-dawn"
        assert parsed["project_id"] == "acme"
        assert parsed["level"] == "info"
        assert "Task started" in (parsed.get("event") or parsed.get("message", ""))

    def test_json_output_no_context_when_unset(self, tmp_path):
        log_file = str(tmp_path / "test.log")
        setup_logging(level="INFO", format="json", log_file=log_file)

        test_logger = logging.getLogger("test.no_context")
        test_logger.info("No context here")

        with open(log_file) as f:
            lines = [line.strip() for line in f if line.strip()]

        parsed = json.loads(lines[-1])
        assert "task_id" not in parsed
        assert "project_id" not in parsed
