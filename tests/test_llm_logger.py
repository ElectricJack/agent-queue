"""Tests for the LLM interaction logger."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone

import pytest

from src.llm_logger import LLMLogger


@pytest.fixture
def log_dir(tmp_path):
    """Provide a temporary directory for log files."""
    return str(tmp_path / "llm_logs")


@pytest.fixture
def logger(log_dir):
    """Create an enabled LLMLogger with a temp directory."""
    return LLMLogger(base_dir=log_dir, enabled=True, retention_days=30)


@pytest.fixture
def disabled_logger(log_dir):
    """Create a disabled LLMLogger."""
    return LLMLogger(base_dir=log_dir, enabled=False, retention_days=30)


class TestLLMLoggerChatProvider:
    def test_writes_valid_jsonl(self, logger, log_dir):
        logger.log_chat_provider_call(
            caller="test",
            model="test-model",
            provider="TestProvider",
            messages=[{"role": "user", "content": "hello"}],
            system="You are helpful.",
            tools=[{"name": "my_tool", "input_schema": {"type": "object"}}],
            max_tokens=512,
            response=None,
            duration_ms=150,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")
        assert os.path.isfile(file_path)

        with open(file_path) as f:
            lines = f.readlines()

        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["caller"] == "test"
        assert entry["model"] == "test-model"
        assert entry["provider"] == "TestProvider"
        assert entry["duration_ms"] == 150

    def test_contains_expected_fields(self, logger, log_dir):
        logger.log_chat_provider_call(
            caller="chat_agent.chat",
            model="claude-sonnet-4-20250514",
            provider="AnthropicChatProvider",
            messages=[
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "4"},
            ],
            system="Be helpful.",
            max_tokens=1024,
            duration_ms=500,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert "timestamp" in entry
        assert entry["caller"] == "chat_agent.chat"
        assert entry["input"]["message_count"] == 2
        assert entry["input"]["system_prompt_length"] == len("Be helpful.")
        assert entry["input"]["max_tokens"] == 1024
        assert entry["duration_ms"] == 500
        assert entry["error"] is None

    def test_logs_tool_names_only(self, logger, log_dir):
        tools = [
            {
                "name": "create_task",
                "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}},
            },
            {"name": "list_tasks", "input_schema": {"type": "object"}},
        ]
        logger.log_chat_provider_call(
            caller="test",
            model="m",
            provider="p",
            messages=[],
            system="s",
            tools=tools,
            duration_ms=0,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["input"]["tool_names"] == ["create_task", "list_tasks"]

    def test_logs_error(self, logger, log_dir):
        logger.log_chat_provider_call(
            caller="test",
            model="m",
            provider="p",
            messages=[],
            system="s",
            error="Connection timeout",
            duration_ms=3000,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["error"] == "Connection timeout"

    def test_multiple_entries_appended(self, logger, log_dir):
        for i in range(3):
            logger.log_chat_provider_call(
                caller=f"test_{i}",
                model="m",
                provider="p",
                messages=[],
                system="s",
                duration_ms=i,
            )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")

        with open(file_path) as f:
            lines = f.readlines()

        assert len(lines) == 3
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["caller"] == f"test_{i}"


class TestLLMLoggerAgentSession:
    def test_writes_agent_session(self, logger, log_dir):
        from dataclasses import dataclass

        @dataclass
        class FakeOutput:
            result: str = "COMPLETED"
            summary: str = "Done"
            tokens_used: int = 5000
            files_changed: list = None
            error_message: str = ""

            def __post_init__(self):
                if self.files_changed is None:
                    self.files_changed = ["src/foo.py"]

        logger.log_agent_session(
            task_id="keen-fox",
            session_id="sess-123",
            model="claude-sonnet",
            prompt="Fix the bug in foo.py",
            config_summary={"allowed_tools": ["Read", "Edit"], "cwd": "/workspace"},
            output=FakeOutput(),
            duration_ms=45000,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "claude_agent.jsonl")
        assert os.path.isfile(file_path)

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["task_id"] == "keen-fox"
        assert entry["session_id"] == "sess-123"
        assert entry["duration_ms"] == 45000
        assert entry["output"]["tokens_used"] == 5000
        assert entry["output"]["result"] == "COMPLETED"
        assert entry["input"]["prompt_length"] == len("Fix the bug in foo.py")


class TestLLMLoggerDisabled:
    def test_disabled_writes_nothing(self, disabled_logger, log_dir):
        disabled_logger.log_chat_provider_call(
            caller="test",
            model="m",
            provider="p",
            messages=[],
            system="s",
            duration_ms=0,
        )
        disabled_logger.log_agent_session(
            task_id="t1",
            prompt="do stuff",
            duration_ms=0,
        )

        # Log directory should not even be created
        assert not os.path.exists(log_dir)


class TestLLMLoggerCleanup:
    def test_removes_old_dirs_keeps_recent(self, log_dir):
        logger = LLMLogger(base_dir=log_dir, enabled=True, retention_days=7)

        # Create fake date directories
        os.makedirs(log_dir, exist_ok=True)
        old_dir = os.path.join(log_dir, "2020-01-01")
        os.makedirs(old_dir)
        with open(os.path.join(old_dir, "chat_provider.jsonl"), "w") as f:
            f.write('{"test": true}\n')

        recent_dir = os.path.join(log_dir, "2099-12-31")
        os.makedirs(recent_dir)
        with open(os.path.join(recent_dir, "chat_provider.jsonl"), "w") as f:
            f.write('{"test": true}\n')

        removed = logger.cleanup_old_logs()

        assert removed == 1
        assert not os.path.exists(old_dir)
        assert os.path.exists(recent_dir)

    def test_cleanup_no_dir(self, log_dir):
        logger = LLMLogger(base_dir=log_dir, enabled=True, retention_days=7)
        removed = logger.cleanup_old_logs()
        assert removed == 0

    def test_cleanup_ignores_non_date_dirs(self, log_dir):
        logger = LLMLogger(base_dir=log_dir, enabled=True, retention_days=7)
        os.makedirs(log_dir, exist_ok=True)

        # Create a non-date directory
        other_dir = os.path.join(log_dir, "not-a-date")
        os.makedirs(other_dir)

        removed = logger.cleanup_old_logs()
        assert removed == 0
        assert os.path.exists(other_dir)
