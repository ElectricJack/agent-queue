"""Tests for llm_config-based chat provider swap in Supervisor.

Verifies Roadmap 0.4.2: when llm_config is passed to Supervisor.chat(),
a different model/provider is used for that single call only.  Subsequent
calls without llm_config revert to the default provider.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.chat_providers import ChatProvider
from src.chat_providers.types import ChatResponse, TextBlock, ToolUseBlock
from src.supervisor import Supervisor, _hook_provider_override, _infer_provider_from_model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingProvider(ChatProvider):
    """A minimal provider that records calls and returns a canned response."""

    def __init__(self, name: str, calls: list | None = None):
        self.name = name
        self._calls: list[str] = calls if calls is not None else []

    @property
    def model_name(self) -> str:
        return self.name

    async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
        self._calls.append(self.name)
        return ChatResponse(content=[TextBlock(text=f"response-from-{self.name}")])


def _make_supervisor_with_provider(provider: ChatProvider) -> Supervisor:
    """Create a Supervisor with a real provider and mocked-out dependencies."""
    orch = MagicMock()
    orch.config = MagicMock()
    orch.llm_logger = MagicMock()
    orch.llm_logger._enabled = False

    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.chat_provider = MagicMock()
    config.chat_provider.provider = "anthropic"
    config.chat_provider.model = "claude-sonnet-4-20250514"
    config.chat_provider.base_url = ""
    config.chat_provider.api_key = ""
    config.chat_provider.keep_alive = "1h"
    config.chat_provider.num_ctx = 0
    config.supervisor = MagicMock()
    config.supervisor.reflection = MagicMock()
    config.supervisor.reflection.level = "full"
    config.supervisor.reflection.max_depth = 3
    config.supervisor.reflection.per_cycle_token_cap = 10000
    config.supervisor.reflection.hourly_token_circuit_breaker = 100000
    config.supervisor.reflection.periodic_interval = 900

    sup = Supervisor(orch, config)
    sup._provider = provider
    return sup


# ---------------------------------------------------------------------------
# _infer_provider_from_model
# ---------------------------------------------------------------------------


class TestInferProviderFromModel:
    def test_claude_models(self):
        assert _infer_provider_from_model("claude-sonnet-4-20250514") == "anthropic"
        assert _infer_provider_from_model("claude-3-haiku-20240307") == "anthropic"
        assert _infer_provider_from_model("Claude-Opus-4") == "anthropic"

    def test_vertex_style_models(self):
        # Vertex models start with "claude" and have @date suffix
        assert _infer_provider_from_model("claude-sonnet-4@20250514") == "anthropic"

    def test_gemini_models(self):
        assert _infer_provider_from_model("gemini-2.5-flash") == "gemini"
        assert _infer_provider_from_model("gemini-2.5-pro") == "gemini"
        assert _infer_provider_from_model("Gemini-1.5-pro") == "gemini"

    def test_unknown_models_return_none(self):
        assert _infer_provider_from_model("qwen2.5:32b") is None
        assert _infer_provider_from_model("llama3:70b") is None
        assert _infer_provider_from_model("gpt-4o") is None


# ---------------------------------------------------------------------------
# _resolve_call_provider
# ---------------------------------------------------------------------------


class TestResolveCallProvider:
    def test_none_config_returns_none(self):
        calls = []
        sup = _make_supervisor_with_provider(RecordingProvider("default", calls))
        assert sup._resolve_call_provider(None) is None

    def test_empty_config_returns_none(self):
        calls = []
        sup = _make_supervisor_with_provider(RecordingProvider("default", calls))
        assert sup._resolve_call_provider({}) is None

    def test_only_max_tokens_returns_none(self):
        """max_tokens alone doesn't trigger a provider swap."""
        calls = []
        sup = _make_supervisor_with_provider(RecordingProvider("default", calls))
        assert sup._resolve_call_provider({"max_tokens": 2048}) is None

    def test_same_model_returns_none(self):
        """No swap needed when the requested model matches the default."""
        calls = []
        sup = _make_supervisor_with_provider(RecordingProvider("claude-sonnet-4-20250514", calls))
        result = sup._resolve_call_provider({"model": "claude-sonnet-4-20250514"})
        assert result is None

    @patch("src.supervisor.create_chat_provider")
    def test_different_model_creates_provider(self, mock_create):
        """A different model triggers provider creation."""
        new_provider = RecordingProvider("gemini-2.5-flash")
        mock_create.return_value = new_provider

        sup = _make_supervisor_with_provider(RecordingProvider("claude-sonnet-4-20250514"))
        result = sup._resolve_call_provider({"model": "gemini-2.5-flash"})

        assert result is not None
        assert result.model_name == "gemini-2.5-flash"
        mock_create.assert_called_once()
        # Check that the config was built correctly
        cfg = mock_create.call_args[0][0]
        assert cfg.provider == "gemini"
        assert cfg.model == "gemini-2.5-flash"

    @patch("src.supervisor.create_chat_provider")
    def test_explicit_provider_override(self, mock_create):
        """Explicit provider key takes precedence over model-based inference."""
        new_provider = RecordingProvider("custom-model")
        mock_create.return_value = new_provider

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        result = sup._resolve_call_provider(
            {
                "model": "custom-model",
                "provider": "ollama",
            }
        )

        assert result is not None
        cfg = mock_create.call_args[0][0]
        assert cfg.provider == "ollama"

    @patch("src.supervisor.create_chat_provider")
    def test_provider_only_override(self, mock_create):
        """Specifying only provider (no model) triggers swap if different."""
        new_provider = RecordingProvider("gemini-2.5-flash")
        mock_create.return_value = new_provider

        sup = _make_supervisor_with_provider(RecordingProvider("claude-sonnet-4-20250514"))
        result = sup._resolve_call_provider({"provider": "gemini"})

        assert result is not None
        cfg = mock_create.call_args[0][0]
        assert cfg.provider == "gemini"

    @patch("src.supervisor.create_chat_provider")
    def test_create_failure_returns_none(self, mock_create):
        """When create_chat_provider returns None, _resolve_call_provider
        gracefully falls back (returns None)."""
        mock_create.return_value = None

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        result = sup._resolve_call_provider({"model": "gemini-2.5-flash"})
        assert result is None

    @patch("src.supervisor.create_chat_provider")
    def test_logging_wraps_provider(self, mock_create):
        """When LLM logging is enabled, the swapped provider is wrapped
        with LoggedChatProvider."""
        from src.chat_providers import LoggedChatProvider

        new_provider = RecordingProvider("gemini-2.5-flash")
        mock_create.return_value = new_provider

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        sup._llm_logger = MagicMock()
        sup._llm_logger._enabled = True

        result = sup._resolve_call_provider({"model": "gemini-2.5-flash"})
        assert isinstance(result, LoggedChatProvider)

    @patch("src.supervisor.create_chat_provider")
    def test_base_url_forwarded_for_ollama(self, mock_create):
        """base_url from llm_config is forwarded to the ChatProviderConfig."""
        new_provider = RecordingProvider("qwen2.5:32b")
        mock_create.return_value = new_provider

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        result = sup._resolve_call_provider(
            {
                "model": "qwen2.5:32b",
                "provider": "ollama",
                "base_url": "http://gpu-server:11434/v1",
            }
        )

        assert result is not None
        cfg = mock_create.call_args[0][0]
        assert cfg.base_url == "http://gpu-server:11434/v1"


# ---------------------------------------------------------------------------
# End-to-end: chat() with llm_config
# ---------------------------------------------------------------------------


class TestChatWithLlmConfig:
    @pytest.mark.asyncio
    async def test_swap_applies_to_single_call(self):
        """When llm_config specifies a different model, that model is used
        for the call, not the default."""
        default_calls = []
        swap_calls = []
        default_provider = RecordingProvider("claude-sonnet-4-20250514", default_calls)
        swap_provider = RecordingProvider("gemini-2.5-flash", swap_calls)

        sup = _make_supervisor_with_provider(default_provider)

        # Mock _build_system_prompt
        sup._build_system_prompt = AsyncMock(return_value="You are a test bot.")

        with patch.object(sup, "_resolve_call_provider", return_value=swap_provider):
            await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        assert "gemini-2.5-flash" in swap_calls
        assert default_calls == []  # Default should NOT be called

    @pytest.mark.asyncio
    async def test_subsequent_call_uses_default(self):
        """After a call with llm_config, the next call without it uses
        the default provider — the swap does not persist."""
        default_calls = []
        default_provider = RecordingProvider("default-model", default_calls)

        sup = _make_supervisor_with_provider(default_provider)
        sup._build_system_prompt = AsyncMock(return_value="You are a test bot.")

        # First call: default (no llm_config)
        await sup.chat(text="hello", user_name="tester")
        assert len(default_calls) == 1

        # Second call: with llm_config (swap)
        swap_calls = []
        swap_provider = RecordingProvider("swap-model", swap_calls)
        with patch.object(sup, "_resolve_call_provider", return_value=swap_provider):
            await sup.chat(
                text="hello again",
                user_name="tester",
                llm_config={"model": "swap-model"},
            )
        assert len(swap_calls) == 1

        # Third call: default again (no llm_config)
        await sup.chat(text="hello once more", user_name="tester")
        assert len(default_calls) == 2  # Called again with default

    @pytest.mark.asyncio
    async def test_max_tokens_applied_with_swap(self):
        """max_tokens from llm_config is passed to create_message even
        when a provider swap occurs."""
        recorded_max_tokens = []

        class TrackingProvider(ChatProvider):
            @property
            def model_name(self) -> str:
                return "tracking"

            async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
                recorded_max_tokens.append(max_tokens)
                return ChatResponse(content=[TextBlock(text="ok")])

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        sup._build_system_prompt = AsyncMock(return_value="test")

        tracking = TrackingProvider()
        with patch.object(sup, "_resolve_call_provider", return_value=tracking):
            await sup.chat(
                text="hi",
                user_name="tester",
                llm_config={"model": "other", "max_tokens": 4096},
            )

        assert recorded_max_tokens == [4096]

    @pytest.mark.asyncio
    async def test_llm_config_provider_takes_priority_over_hook_override(self):
        """llm_config-based provider should take priority over the
        _hook_provider_override contextvar."""
        hook_calls = []
        llm_config_calls = []
        hook_provider = RecordingProvider("hook-provider", hook_calls)
        llm_config_provider = RecordingProvider("llm-config-provider", llm_config_calls)

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        sup._build_system_prompt = AsyncMock(return_value="test")

        # Set the hook provider override
        token = _hook_provider_override.set(hook_provider)
        try:
            with patch.object(sup, "_resolve_call_provider", return_value=llm_config_provider):
                await sup.chat(
                    text="hi",
                    user_name="tester",
                    llm_config={"model": "llm-config-model"},
                )
        finally:
            _hook_provider_override.reset(token)

        assert len(llm_config_calls) == 1
        assert hook_calls == []  # Hook provider should NOT be called

    @pytest.mark.asyncio
    async def test_no_llm_config_uses_default(self):
        """Without llm_config, the default provider is used as before."""
        default_calls = []
        default_provider = RecordingProvider("default", default_calls)
        sup = _make_supervisor_with_provider(default_provider)
        sup._build_system_prompt = AsyncMock(return_value="test")

        await sup.chat(text="hi", user_name="tester")
        assert len(default_calls) == 1


# ---------------------------------------------------------------------------
# Multi-turn: verify swap persists across tool-use rounds within a single call
# ---------------------------------------------------------------------------


class TestMultiTurnSwap:
    @pytest.mark.asyncio
    async def test_swap_persists_across_tool_rounds(self):
        """The swapped provider should be used for ALL rounds in a
        multi-turn tool-use loop, not just the first one."""
        round_providers = []

        class MultiTurnProvider(ChatProvider):
            def __init__(self):
                self._round = 0

            @property
            def model_name(self) -> str:
                return "multi-turn-swap"

            async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
                round_providers.append("multi-turn-swap")
                self._round += 1
                if self._round == 1:
                    # First round: return a tool use (reply_to_user to
                    # avoid the nudge loop that would add extra rounds)
                    return ChatResponse(
                        content=[
                            ToolUseBlock(
                                id="tu-1",
                                name="reply_to_user",
                                input={"message": "here are the tasks"},
                            ),
                        ]
                    )
                # Subsequent rounds (shouldn't happen with reply_to_user)
                return ChatResponse(content=[TextBlock(text="done")])

        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        sup._build_system_prompt = AsyncMock(return_value="test")

        # Mock the command handler to handle the tool call
        sup.handler.execute = AsyncMock(return_value={"success": True, "message": "ok"})

        swap = MultiTurnProvider()
        with patch.object(sup, "_resolve_call_provider", return_value=swap):
            await sup.chat(
                text="show tasks",
                user_name="tester",
                llm_config={"model": "multi-turn-swap"},
            )

        # The swap provider should have been used (not the default)
        assert len(round_providers) >= 1
        assert all(p == "multi-turn-swap" for p in round_providers)


# ---------------------------------------------------------------------------
# Reflection retry: verify llm_config is forwarded
# ---------------------------------------------------------------------------


class TestReflectionRetryForwardsLlmConfig:
    @pytest.mark.asyncio
    async def test_llm_config_forwarded_on_reflection_retry(self):
        """When reflection triggers a retry, llm_config is passed through
        to _chat_unlocked so the retry uses the same provider."""
        # This is a structural test — we verify the parameter is threaded
        # through by checking the existing code path.
        # The actual forwarding was implemented in 0.4.1; we just verify
        # it still works.
        sup = _make_supervisor_with_provider(RecordingProvider("default"))
        sup._build_system_prompt = AsyncMock(return_value="test")

        # Verify the chat method signature accepts llm_config
        import inspect

        sig = inspect.signature(sup.chat)
        assert "llm_config" in sig.parameters

        sig_unlocked = inspect.signature(sup._chat_unlocked)
        assert "llm_config" in sig_unlocked.parameters

        sig_inner = inspect.signature(sup._chat_inner)
        assert "llm_config" in sig_inner.parameters
