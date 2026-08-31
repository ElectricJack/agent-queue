"""Tests for the LLM interaction logger."""

import json
import os
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


class TestLLMLoggerCall:
    def test_writes_valid_jsonl(self, logger, log_dir):
        logger.log_llm_call(
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
        file_path = os.path.join(log_dir, today, "llm.jsonl")
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
        logger.log_llm_call(
            caller="playbook_node.chat",
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
        file_path = os.path.join(log_dir, today, "llm.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert "timestamp" in entry
        assert entry["caller"] == "playbook_node.chat"
        assert len(entry["input"]["messages"]) == 2
        assert entry["input"]["system"] == "Be helpful."
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
        logger.log_llm_call(
            caller="test",
            model="m",
            provider="p",
            messages=[],
            system="s",
            tools=tools,
            duration_ms=0,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "llm.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["input"]["tool_names"] == ["create_task", "list_tasks"]

    def test_logs_error(self, logger, log_dir):
        logger.log_llm_call(
            caller="test",
            model="m",
            provider="p",
            messages=[],
            system="s",
            error="Connection timeout",
            duration_ms=3000,
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "llm.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["error"] == "Connection timeout"

    def test_multiple_entries_appended(self, logger, log_dir):
        for i in range(3):
            logger.log_llm_call(
                caller=f"test_{i}",
                model="m",
                provider="p",
                messages=[],
                system="s",
                duration_ms=i,
            )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "llm.jsonl")

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
        disabled_logger.log_llm_call(
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
        with open(os.path.join(old_dir, "llm.jsonl"), "w") as f:
            f.write('{"test": true}\n')

        recent_dir = os.path.join(log_dir, "2099-12-31")
        os.makedirs(recent_dir)
        with open(os.path.join(recent_dir, "llm.jsonl"), "w") as f:
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


class TestLLMLoggerObservabilityContract:
    """Response summaries, analytics, and per-task copies (plan §intelligence 15-17)."""

    @staticmethod
    def _read(log_dir, filename):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(log_dir, today, filename)
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_provider_call_records_response_tool_summary_fingerprint_and_estimates(
        self, logger, log_dir
    ):
        from src.llm.types import ChatResponse, TextBlock, ToolUseBlock

        response = ChatResponse(
            content=[
                TextBlock(text="a" * 40),
                ToolUseBlock(id="tu_1", name="list_tasks", input={"project_id": "p"}),
                ToolUseBlock(id="tu_2", name="close_task", input={"task_id": "t"}),
            ]
        )
        system = "s" * 120
        messages = [{"role": "user", "content": "u" * 60}]
        # Tool order differs between the two calls; the fingerprint must not.
        tools_a = [{"name": "zeta"}, {"name": "alpha"}]
        tools_b = [{"name": "alpha"}, {"name": "zeta"}]

        for tools in (tools_a, tools_b):
            logger.log_llm_call(
                caller="reflection",
                model="claude-opus-5",
                provider="AnthropicProvider",
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=2048,
                response=response,
                duration_ms=1234,
            )

        first, second = self._read(log_dir, "llm.jsonl")

        assert first["output"]["text_parts"] == ["a" * 40]
        assert first["output"]["tool_uses"] == [
            {"name": "list_tasks", "input": {"project_id": "p"}},
            {"name": "close_task", "input": {"task_id": "t"}},
        ]
        # Rough 4-chars-per-token estimates over system + message content.
        assert first["input"]["input_tokens_est"] == (120 + 60) // 4
        assert first["output"]["output_tokens_est"] == 40 // 4
        assert first["input"]["tool_names"] == ["zeta", "alpha"]
        assert first["error"] is None
        assert first["duration_ms"] == 1234

        # The fingerprint keys on the sorted tool names, so it is stable
        # across call-order noise — that is what makes A/B comparison work.
        assert first["prompt_fingerprint"] == second["prompt_fingerprint"]
        assert len(first["prompt_fingerprint"]) == 12

        # A different system prompt gives a different fingerprint.
        logger.log_llm_call(
            caller="reflection",
            model="claude-opus-5",
            provider="AnthropicProvider",
            messages=messages,
            system="a completely different system prompt",
            tools=tools_a,
            response=response,
        )
        third = self._read(log_dir, "llm.jsonl")[2]
        assert third["prompt_fingerprint"] != first["prompt_fingerprint"]

    def test_flush_analytics_writes_rates_and_resets_accumulator(self, logger, log_dir):
        from src.llm.types import ChatResponse, TextBlock

        common = {
            "caller": "playbook",
            "model": "gpt-5",
            "provider": "OpenAIProvider",
            "messages": [{"role": "user", "content": "u" * 40}],
            "system": "s" * 40,
        }
        logger.log_llm_call(
            **common,
            response=ChatResponse(content=[TextBlock(text="o" * 20)]),
            duration_ms=100,
        )
        logger.log_llm_call(**common, response=None, error="boom", duration_ms=300)

        logger.flush_analytics()

        (entry,) = self._read(log_dir, "prompt_analytics.jsonl")
        stats = entry["callers"]["playbook:OpenAIProvider:gpt-5"]
        assert entry["period_type"] == "flush"
        assert stats["call_count"] == 2
        assert stats["error_count"] == 1
        assert stats["error_rate"] == 0.5
        assert stats["avg_duration_ms"] == 200
        assert stats["total_input_tokens_est"] == 2 * ((40 + 40) // 4)
        assert stats["total_output_tokens_est"] == 20 // 4
        assert stats["token_efficiency"] == pytest.approx(
            stats["total_output_tokens_est"] / stats["total_input_tokens_est"]
        )

        # The accumulator resets so the next window starts clean and a second
        # flush writes nothing.
        assert logger.get_analytics_summary() == {}
        logger.flush_analytics()
        assert len(self._read(log_dir, "prompt_analytics.jsonl")) == 1

    def test_agent_session_writes_per_task_copy_and_truncates_error(self, logger, log_dir):
        from dataclasses import dataclass, field

        @dataclass
        class FakeOutput:
            result: str = "FAILED"
            summary: str = "did not finish"
            tokens_used: int = 1234
            files_changed: list = field(default_factory=list)
            error_message: str = "E" * 600

        logger.log_agent_session(
            task_id="noble-stone.24",
            session_id="sess-1",
            model="claude-opus-5",
            prompt="p" * 700,
            output=FakeOutput(),
            duration_ms=9000,
            transcript=[{"type": "assistant", "text": "working"}],
        )

        (main_entry,) = self._read(log_dir, "claude_agent.jsonl")
        (task_entry,) = self._read(log_dir, os.path.join("tasks", "noble-stone.24.jsonl"))

        # The per-task copy is byte-identical to the shared log entry.
        assert task_entry == main_entry
        assert main_entry["task_id"] == "noble-stone.24"
        assert main_entry["input"]["prompt_length"] == 700
        assert main_entry["transcript"] == [{"type": "assistant", "text": "working"}]
        # Errors are truncated so one runaway traceback cannot dominate a log.
        assert main_entry["output"]["error"] == "E" * 500
        assert main_entry["output"]["result"] == "FAILED"
