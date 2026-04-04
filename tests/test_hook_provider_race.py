"""Tests for the provider race condition fix in hook LLM invocation.

Verifies that concurrent hooks with different llm_config overrides each
use their own provider instead of racing on a shared attribute.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.chat_providers import ChatProvider
from src.chat_providers.types import ChatResponse, TextBlock
from src.supervisor import Supervisor, _hook_provider_override


class RecordingProvider(ChatProvider):
    """A provider that records which instance handled each call."""

    def __init__(self, name: str, calls: list):
        self.name = name
        self._calls = calls

    @property
    def model_name(self) -> str:
        return self.name

    async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
        self._calls.append(self.name)
        # Small delay to widen the race window
        await asyncio.sleep(0.05)
        return ChatResponse(content=[TextBlock(text="ok")])


class TestProviderContextVar:
    @pytest.mark.asyncio
    async def test_concurrent_hooks_use_own_provider(self):
        """Two concurrent process_hook_llm calls with different providers
        should each use their own provider, not corrupt each other."""
        calls = []
        default_provider = RecordingProvider("default", calls)
        provider_a = RecordingProvider("hook-a", calls)
        provider_b = RecordingProvider("hook-b", calls)

        supervisor = MagicMock(spec=Supervisor)
        supervisor._provider = default_provider

        # Use the real process_hook_llm but mock chat() to just call the provider
        async def fake_chat(text, user_name, on_progress=None, _reflection_trigger=None):
            active = _hook_provider_override.get() or supervisor._provider
            resp = await active.create_message(messages=[], system="", tools=[], max_tokens=1024)
            return resp.text_parts[0]

        supervisor.chat = fake_chat
        supervisor.set_active_project = MagicMock()
        supervisor.process_hook_llm = Supervisor.process_hook_llm.__get__(supervisor)

        # Launch both concurrently
        results = await asyncio.gather(
            supervisor.process_hook_llm(
                hook_context="ctx-a",
                rendered_prompt="prompt-a",
                project_id="p1",
                hook_name="hook-a",
                provider=provider_a,
            ),
            supervisor.process_hook_llm(
                hook_context="ctx-b",
                rendered_prompt="prompt-b",
                project_id="p2",
                hook_name="hook-b",
                provider=provider_b,
            ),
        )

        assert results == ["ok", "ok"]
        # Each hook must have used its own provider
        assert "hook-a" in calls
        assert "hook-b" in calls
        # Default provider should NOT have been called
        assert "default" not in calls

    @pytest.mark.asyncio
    async def test_no_provider_override_uses_default(self):
        """When no provider override is passed, the default provider is used."""
        calls = []
        default_provider = RecordingProvider("default", calls)

        supervisor = MagicMock(spec=Supervisor)
        supervisor._provider = default_provider

        async def fake_chat(text, user_name, on_progress=None, _reflection_trigger=None):
            active = _hook_provider_override.get() or supervisor._provider
            resp = await active.create_message(messages=[], system="", tools=[], max_tokens=1024)
            return resp.text_parts[0]

        supervisor.chat = fake_chat
        supervisor.set_active_project = MagicMock()
        supervisor.process_hook_llm = Supervisor.process_hook_llm.__get__(supervisor)

        result = await supervisor.process_hook_llm(
            hook_context="ctx",
            rendered_prompt="prompt",
            hook_name="no-override",
        )

        assert result == "ok"
        assert calls == ["default"]

    @pytest.mark.asyncio
    async def test_contextvar_cleaned_up_after_call(self):
        """The contextvar should be reset after process_hook_llm returns."""
        calls = []
        provider = RecordingProvider("custom", calls)

        supervisor = MagicMock(spec=Supervisor)
        supervisor._provider = RecordingProvider("default", calls)

        async def fake_chat(text, user_name, on_progress=None, _reflection_trigger=None):
            return "ok"

        supervisor.chat = fake_chat
        supervisor.set_active_project = MagicMock()
        supervisor.process_hook_llm = Supervisor.process_hook_llm.__get__(supervisor)

        await supervisor.process_hook_llm(
            hook_context="ctx",
            rendered_prompt="prompt",
            provider=provider,
        )

        # After the call, the contextvar should be back to None
        assert _hook_provider_override.get() is None
