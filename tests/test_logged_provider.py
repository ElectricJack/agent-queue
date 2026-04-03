"""Tests for the LoggedChatProvider wrapper."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.chat_providers.logged import LoggedChatProvider
from src.chat_providers.types import ChatResponse, TextBlock
from src.llm_logger import LLMLogger


@pytest.fixture
def log_dir(tmp_path):
    return str(tmp_path / "llm_logs")


@pytest.fixture
def logger(log_dir):
    return LLMLogger(base_dir=log_dir, enabled=True, retention_days=30)


@pytest.fixture
def mock_provider():
    provider = AsyncMock()
    provider.model_name = "test-model-v1"
    provider.create_message = AsyncMock()
    return provider


class TestLoggedChatProviderDelegation:
    async def test_delegates_to_inner_provider(self, mock_provider, logger):
        expected_response = ChatResponse(content=[TextBlock(text="Hello!")])
        mock_provider.create_message.return_value = expected_response

        logged = LoggedChatProvider(mock_provider, logger, caller="test")
        result = await logged.create_message(
            messages=[{"role": "user", "content": "Hi"}],
            system="Be nice.",
            tools=[{"name": "foo", "input_schema": {}}],
            max_tokens=256,
        )

        assert result == expected_response
        mock_provider.create_message.assert_called_once_with(
            messages=[{"role": "user", "content": "Hi"}],
            system="Be nice.",
            tools=[{"name": "foo", "input_schema": {}}],
            max_tokens=256,
        )

    async def test_model_name_delegates(self, mock_provider, logger):
        logged = LoggedChatProvider(mock_provider, logger)
        assert logged.model_name == "test-model-v1"


class TestLoggedChatProviderLogging:
    async def test_logs_on_success(self, mock_provider, logger, log_dir):
        response = ChatResponse(content=[TextBlock(text="Answer")])
        mock_provider.create_message.return_value = response

        logged = LoggedChatProvider(mock_provider, logger, caller="chat_agent.chat")
        await logged.create_message(
            messages=[{"role": "user", "content": "question"}],
            system="sys",
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")
        assert os.path.isfile(file_path)

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["caller"] == "chat_agent.chat"
        assert entry["model"] == "test-model-v1"
        assert entry["error"] is None
        assert entry["duration_ms"] >= 0
        assert entry["output"]["text_parts"] == ["Answer"]

    async def test_logs_on_error(self, mock_provider, logger, log_dir):
        mock_provider.create_message.side_effect = RuntimeError("API down")

        logged = LoggedChatProvider(mock_provider, logger, caller="hook_engine")

        with pytest.raises(RuntimeError, match="API down"):
            await logged.create_message(
                messages=[],
                system="sys",
            )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")
        assert os.path.isfile(file_path)

        with open(file_path) as f:
            entry = json.loads(f.readline())

        assert entry["caller"] == "hook_engine"
        assert entry["error"] == "API down"

    async def test_timing_recorded(self, mock_provider, logger, log_dir):
        import asyncio

        async def slow_call(**kwargs):
            await asyncio.sleep(0.05)
            return ChatResponse(content=[TextBlock(text="done")])

        mock_provider.create_message.side_effect = slow_call

        logged = LoggedChatProvider(mock_provider, logger, caller="test")
        await logged.create_message(messages=[], system="s")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")

        with open(file_path) as f:
            entry = json.loads(f.readline())

        # Should be at least 50ms
        assert entry["duration_ms"] >= 40  # allow some slack

    async def test_caller_can_be_changed(self, mock_provider, logger, log_dir):
        response = ChatResponse(content=[TextBlock(text="ok")])
        mock_provider.create_message.return_value = response

        logged = LoggedChatProvider(mock_provider, logger, caller="chat_agent.chat")

        # First call with default caller
        await logged.create_message(messages=[], system="s")

        # Change caller
        logged._caller = "chat_agent.summarize"
        await logged.create_message(messages=[], system="s")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        file_path = os.path.join(log_dir, today, "chat_provider.jsonl")

        with open(file_path) as f:
            lines = f.readlines()

        assert len(lines) == 2
        assert json.loads(lines[0])["caller"] == "chat_agent.chat"
        assert json.loads(lines[1])["caller"] == "chat_agent.summarize"
