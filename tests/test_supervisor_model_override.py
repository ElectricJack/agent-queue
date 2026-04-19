"""Tests for Supervisor model override via llm_config (Roadmap 0.4.4).

Verifies that the Supervisor.chat() method correctly handles llm_config-based
model and provider overrides:

(a) llm_config with a different model routes to the correct provider
(b) No llm_config uses the default model/provider (backward compat)
(c) llm_config with an invalid/unknown model is handled gracefully
(d) Model override applies only to that single call -- subsequent calls revert
(e) Additional parameters (max_tokens) in llm_config are passed through
(f) Concurrent calls with different llm_config don't interfere
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.chat_providers import ChatProvider
from src.chat_providers.types import ChatResponse, TextBlock
from src.supervisor import Supervisor, _infer_provider_from_model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingProvider(ChatProvider):
    """Minimal provider that records every create_message call."""

    def __init__(self, name: str, calls: list | None = None):
        self.name = name
        self._calls: list[dict] = calls if calls is not None else []

    @property
    def model_name(self) -> str:
        return self.name

    async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
        self._calls.append({"model": self.name, "max_tokens": max_tokens})
        return ChatResponse(content=[TextBlock(text=f"response-from-{self.name}")])


def _make_supervisor(
    provider_name: str = "claude-sonnet-4-20250514",
    provider_type: str = "anthropic",
    calls: list | None = None,
) -> tuple[Supervisor, RecordingProvider, list]:
    """Create a Supervisor with a RecordingProvider and mocked dependencies.

    Returns (supervisor, default_provider, calls_list).
    """
    call_log = calls if calls is not None else []
    provider = RecordingProvider(provider_name, call_log)

    orch = MagicMock()
    orch.config = MagicMock()
    orch.llm_logger = MagicMock()
    orch.llm_logger._enabled = False

    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.chat_provider = MagicMock()
    config.chat_provider.provider = provider_type
    config.chat_provider.model = provider_name
    config.chat_provider.base_url = ""
    config.chat_provider.api_key = ""
    config.chat_provider.keep_alive = "1h"
    config.chat_provider.num_ctx = 0
    # Supervisor reads config.chat_provider.max_tokens as the default budget.
    config.chat_provider.max_tokens = 1024
    config.supervisor = MagicMock()
    config.supervisor.reflection = MagicMock()
    config.supervisor.reflection.level = "full"
    config.supervisor.reflection.max_depth = 3
    config.supervisor.reflection.per_cycle_token_cap = 10000
    config.supervisor.reflection.hourly_token_circuit_breaker = 100000
    config.supervisor.reflection.periodic_interval = 900

    sup = Supervisor(orch, config)
    sup._provider = provider
    sup._build_system_prompt = AsyncMock(return_value="You are a test bot.")
    return sup, provider, call_log


# ---------------------------------------------------------------------------
# (a) chat() with llm_config={"model": "different-model"} routes correctly
# ---------------------------------------------------------------------------


class TestModelOverrideRouting:
    """Verify that llm_config={"model": ...} routes to the correct provider."""

    @pytest.mark.asyncio
    async def test_routes_to_gemini_provider(self):
        """llm_config with a Gemini model creates and uses a Gemini provider."""
        sup, default_provider, default_calls = _make_supervisor()

        swap_calls: list[dict] = []
        swap_provider = RecordingProvider("gemini-2.5-flash", swap_calls)

        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            result = await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        # Swap provider was called
        assert len(swap_calls) == 1
        assert swap_calls[0]["model"] == "gemini-2.5-flash"
        # Default provider was NOT called
        assert default_calls == []
        assert "gemini-2.5-flash" in result

    @pytest.mark.asyncio
    async def test_routes_to_ollama_provider(self):
        """llm_config with provider='ollama' creates an Ollama provider."""
        sup, _, default_calls = _make_supervisor()

        swap_calls: list[dict] = []
        swap_provider = RecordingProvider("qwen2.5:32b", swap_calls)

        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={
                    "model": "qwen2.5:32b",
                    "provider": "ollama",
                    "base_url": "http://localhost:11434/v1",
                },
            )

        assert len(swap_calls) == 1
        assert swap_calls[0]["model"] == "qwen2.5:32b"
        assert default_calls == []

    @pytest.mark.asyncio
    async def test_routes_to_different_anthropic_model(self):
        """llm_config with a different Claude model creates a new provider."""
        sup, _, default_calls = _make_supervisor()

        swap_calls: list[dict] = []
        swap_provider = RecordingProvider("claude-opus-4-20250514", swap_calls)

        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "claude-opus-4-20250514"},
            )

        # Swapped to a different Anthropic model
        assert len(swap_calls) == 1
        assert swap_calls[0]["model"] == "claude-opus-4-20250514"
        assert default_calls == []

    @pytest.mark.asyncio
    async def test_resolve_provider_config_has_correct_type(self):
        """When model is 'gemini-*', the ChatProviderConfig.provider is 'gemini'."""
        sup, _, _ = _make_supervisor()

        with patch("src.supervisor.create_chat_provider") as mock_create:
            mock_create.return_value = RecordingProvider("gemini-2.5-flash")
            sup._resolve_call_provider({"model": "gemini-2.5-flash"})

            cfg = mock_create.call_args[0][0]
            assert cfg.provider == "gemini"
            assert cfg.model == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_explicit_provider_overrides_inferred(self):
        """An explicit 'provider' key overrides what would be inferred from model."""
        sup, _, _ = _make_supervisor()

        with patch("src.supervisor.create_chat_provider") as mock_create:
            mock_create.return_value = RecordingProvider("some-model")
            sup._resolve_call_provider(
                {
                    "model": "some-model",
                    "provider": "ollama",
                }
            )

            cfg = mock_create.call_args[0][0]
            # Even though "some-model" would infer None, explicit provider wins
            assert cfg.provider == "ollama"


# ---------------------------------------------------------------------------
# (b) chat() without llm_config uses default (backward compatibility)
# ---------------------------------------------------------------------------


class TestDefaultProviderBackwardCompat:
    """Verify that calls without llm_config use the configured default provider."""

    @pytest.mark.asyncio
    async def test_no_llm_config_uses_default_provider(self):
        """Without llm_config, the default provider handles the request."""
        sup, _, default_calls = _make_supervisor()

        result = await sup.chat(text="hello", user_name="tester")

        assert len(default_calls) == 1
        assert default_calls[0]["model"] == "claude-sonnet-4-20250514"
        assert "claude-sonnet-4-20250514" in result

    @pytest.mark.asyncio
    async def test_none_llm_config_uses_default(self):
        """Explicitly passing llm_config=None is the same as omitting it."""
        sup, _, default_calls = _make_supervisor()

        await sup.chat(text="hello", user_name="tester", llm_config=None)

        assert len(default_calls) == 1

    @pytest.mark.asyncio
    async def test_empty_llm_config_uses_default(self):
        """An empty llm_config dict does not trigger a provider swap."""
        sup, _, default_calls = _make_supervisor()

        await sup.chat(text="hello", user_name="tester", llm_config={})

        assert len(default_calls) == 1
        assert default_calls[0]["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_same_model_in_llm_config_uses_default(self):
        """llm_config with the same model as default doesn't create a new provider."""
        sup, _, default_calls = _make_supervisor()

        # Request the same model that's already the default
        await sup.chat(
            text="hello",
            user_name="tester",
            llm_config={"model": "claude-sonnet-4-20250514"},
        )

        # Should still use the default provider (short-circuit in _resolve_call_provider)
        assert len(default_calls) == 1

    @pytest.mark.asyncio
    async def test_default_max_tokens_when_no_llm_config(self):
        """Without llm_config, the default max_tokens=1024 is used."""
        sup, _, default_calls = _make_supervisor()

        await sup.chat(text="hello", user_name="tester")

        assert default_calls[0]["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# (c) llm_config with invalid/unknown model is handled gracefully
# ---------------------------------------------------------------------------


class TestInvalidModelHandling:
    """Verify graceful behavior when llm_config specifies an unknown model."""

    @pytest.mark.asyncio
    async def test_unknown_model_falls_back_to_default_provider_type(self):
        """An unknown model (e.g. 'gpt-4o') falls back to the configured
        provider type since _infer_provider_from_model returns None."""
        sup, _, _ = _make_supervisor()

        with patch("src.supervisor.create_chat_provider") as mock_create:
            mock_create.return_value = RecordingProvider("gpt-4o")
            sup._resolve_call_provider({"model": "gpt-4o"})

            # Provider config uses the default provider type ("anthropic")
            # since inference returns None for unknown models
            cfg = mock_create.call_args[0][0]
            assert cfg.provider == "anthropic"
            assert cfg.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_provider_creation_failure_falls_back_to_default(self):
        """When create_chat_provider returns None (e.g., missing API key),
        the system gracefully falls back to the default provider."""
        sup, _, default_calls = _make_supervisor()

        with patch("src.supervisor.create_chat_provider", return_value=None):
            result = await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        # _resolve_call_provider returns None, so default provider is used
        assert len(default_calls) == 1
        assert "claude-sonnet-4-20250514" in result

    def test_infer_provider_returns_none_for_unknown_models(self):
        """_infer_provider_from_model returns None for non-Claude/Gemini models."""
        assert _infer_provider_from_model("gpt-4o") is None
        assert _infer_provider_from_model("llama3:70b") is None
        assert _infer_provider_from_model("mistral-large") is None
        assert _infer_provider_from_model("qwen2.5:32b") is None

    def test_resolve_returns_none_when_only_max_tokens(self):
        """llm_config with only max_tokens (no model/provider) does not swap."""
        sup, _, _ = _make_supervisor()
        assert sup._resolve_call_provider({"max_tokens": 4096}) is None

    def test_resolve_returns_none_when_only_temperature(self):
        """llm_config with only temperature (no model/provider) does not swap."""
        sup, _, _ = _make_supervisor()
        assert sup._resolve_call_provider({"temperature": 0.7}) is None


# ---------------------------------------------------------------------------
# (d) Model override applies only to that single call
# ---------------------------------------------------------------------------


class TestOverrideSingleCallScope:
    """Verify the model override is scoped to a single chat() call."""

    @pytest.mark.asyncio
    async def test_override_does_not_persist_to_next_call(self):
        """After a call with llm_config, the next call without it reverts
        to the default provider."""
        sup, _, default_calls = _make_supervisor()

        swap_calls: list[dict] = []
        swap_provider = RecordingProvider("gemini-2.5-flash", swap_calls)

        # Call 1: with llm_config (uses swap provider)
        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            await sup.chat(
                text="first",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        assert len(swap_calls) == 1
        assert len(default_calls) == 0

        # Call 2: without llm_config (should revert to default)
        await sup.chat(text="second", user_name="tester")

        assert len(default_calls) == 1
        assert default_calls[0]["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_alternating_overrides(self):
        """Alternating between different models and default works correctly."""
        sup, _, default_calls = _make_supervisor()

        gemini_calls: list[dict] = []
        gemini_provider = RecordingProvider("gemini-2.5-flash", gemini_calls)

        ollama_calls: list[dict] = []
        ollama_provider = RecordingProvider("qwen2.5:32b", ollama_calls)

        # Call 1: default
        await sup.chat(text="msg1", user_name="tester")
        assert len(default_calls) == 1

        # Call 2: gemini override
        with patch("src.supervisor.create_chat_provider", return_value=gemini_provider):
            await sup.chat(
                text="msg2",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )
        assert len(gemini_calls) == 1

        # Call 3: default again
        await sup.chat(text="msg3", user_name="tester")
        assert len(default_calls) == 2

        # Call 4: ollama override
        with patch("src.supervisor.create_chat_provider", return_value=ollama_provider):
            await sup.chat(
                text="msg4",
                user_name="tester",
                llm_config={"model": "qwen2.5:32b", "provider": "ollama"},
            )
        assert len(ollama_calls) == 1

        # Call 5: default once more
        await sup.chat(text="msg5", user_name="tester")
        assert len(default_calls) == 3

        # No cross-contamination
        assert len(gemini_calls) == 1
        assert len(ollama_calls) == 1

    @pytest.mark.asyncio
    async def test_provider_not_stored_on_supervisor(self):
        """The swap provider is not stored on self._provider -- it remains
        the original default after a swap call."""
        sup, default_provider, _ = _make_supervisor()

        swap_provider = RecordingProvider("gemini-2.5-flash")

        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        # self._provider is still the original default
        assert sup._provider is default_provider
        assert sup._provider.model_name == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# (e) Additional parameters (max_tokens) are passed through
# ---------------------------------------------------------------------------


class TestAdditionalParameterPassthrough:
    """Verify that max_tokens and other llm_config params are forwarded."""

    @pytest.mark.asyncio
    async def test_max_tokens_passed_to_create_message(self):
        """max_tokens from llm_config is forwarded to the provider."""
        sup, _, default_calls = _make_supervisor()

        await sup.chat(
            text="hello",
            user_name="tester",
            llm_config={"max_tokens": 8192},
        )

        # No model swap (only max_tokens), so default provider is used
        assert len(default_calls) == 1
        assert default_calls[0]["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_max_tokens_with_model_override(self):
        """max_tokens is forwarded even when combined with a model swap."""
        sup, _, default_calls = _make_supervisor()

        swap_calls: list[dict] = []
        swap_provider = RecordingProvider("gemini-2.5-flash", swap_calls)

        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash", "max_tokens": 4096},
            )

        assert len(swap_calls) == 1
        assert swap_calls[0]["max_tokens"] == 4096
        assert default_calls == []

    @pytest.mark.asyncio
    async def test_default_max_tokens_when_not_specified_in_llm_config(self):
        """When llm_config has a model but no max_tokens, default 1024 is used."""
        sup, _, _ = _make_supervisor()

        swap_calls: list[dict] = []
        swap_provider = RecordingProvider("gemini-2.5-flash", swap_calls)

        with patch("src.supervisor.create_chat_provider", return_value=swap_provider):
            await sup.chat(
                text="hello",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        assert swap_calls[0]["max_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_base_url_forwarded_to_provider_config(self):
        """base_url from llm_config is passed to the ChatProviderConfig."""
        sup, _, _ = _make_supervisor()

        with patch("src.supervisor.create_chat_provider") as mock_create:
            mock_create.return_value = RecordingProvider("qwen2.5:32b")
            sup._resolve_call_provider(
                {
                    "model": "qwen2.5:32b",
                    "provider": "ollama",
                    "base_url": "http://gpu-server:11434/v1",
                }
            )

            cfg = mock_create.call_args[0][0]
            assert cfg.base_url == "http://gpu-server:11434/v1"

    @pytest.mark.asyncio
    async def test_api_key_forwarded_to_provider_config(self):
        """api_key from llm_config is passed to the ChatProviderConfig."""
        sup, _, _ = _make_supervisor()

        with patch("src.supervisor.create_chat_provider") as mock_create:
            mock_create.return_value = RecordingProvider("gemini-2.5-flash")
            sup._resolve_call_provider(
                {
                    "model": "gemini-2.5-flash",
                    "api_key": "custom-api-key-123",
                }
            )

            cfg = mock_create.call_args[0][0]
            assert cfg.api_key == "custom-api-key-123"


# ---------------------------------------------------------------------------
# (f) Concurrent calls with different llm_config don't interfere
# ---------------------------------------------------------------------------


class TestConcurrentCallIsolation:
    """Verify that concurrent/sequential calls with different llm_config
    do not interfere with each other's provider resolution."""

    @pytest.mark.asyncio
    async def test_sequential_calls_use_correct_providers(self):
        """Multiple sequential calls with different llm_config each use
        the correct provider -- no state leakage between calls."""
        sup, _, default_calls = _make_supervisor()

        gemini_calls: list[dict] = []
        gemini_provider = RecordingProvider("gemini-2.5-flash", gemini_calls)

        ollama_calls: list[dict] = []
        ollama_provider = RecordingProvider("qwen2.5:32b", ollama_calls)

        # Call 1: Gemini
        with patch("src.supervisor.create_chat_provider", return_value=gemini_provider):
            await sup.chat(
                text="gemini task",
                user_name="tester",
                llm_config={"model": "gemini-2.5-flash"},
            )

        # Call 2: Ollama
        with patch("src.supervisor.create_chat_provider", return_value=ollama_provider):
            await sup.chat(
                text="ollama task",
                user_name="tester",
                llm_config={"model": "qwen2.5:32b", "provider": "ollama"},
            )

        # Call 3: Default
        await sup.chat(text="default task", user_name="tester")

        # Each call used the correct provider
        assert len(gemini_calls) == 1
        assert gemini_calls[0]["model"] == "gemini-2.5-flash"
        assert len(ollama_calls) == 1
        assert ollama_calls[0]["model"] == "qwen2.5:32b"
        assert len(default_calls) == 1
        assert default_calls[0]["model"] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_concurrent_chat_inner_calls_isolated(self):
        """_chat_inner resolves call_provider as a local variable, so
        concurrent invocations (bypassing the lock for testing) use
        independent provider instances with no shared state."""
        sup, _, default_calls = _make_supervisor()

        gemini_calls: list[dict] = []
        ollama_calls: list[dict] = []

        # Create slow providers that yield control mid-execution
        class SlowProvider(ChatProvider):
            def __init__(self, name: str, calls: list, delay: float = 0.05):
                self.name = name
                self._calls = calls
                self._delay = delay

            @property
            def model_name(self) -> str:
                return self.name

            async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
                # Yield control to allow interleaving
                await asyncio.sleep(self._delay)
                self._calls.append({"model": self.name, "max_tokens": max_tokens})
                return ChatResponse(content=[TextBlock(text=f"response-from-{self.name}")])

        gemini_provider = SlowProvider("gemini-2.5-flash", gemini_calls, delay=0.05)
        ollama_provider = SlowProvider("qwen2.5:32b", ollama_calls, delay=0.05)

        # Call _chat_inner directly (bypasses the _llm_lock) to test true concurrency
        async def call_with_swap(provider, llm_config):
            with patch.object(sup, "_resolve_call_provider", return_value=provider):
                return await sup._chat_inner(
                    text="hello",
                    user_name="tester",
                    llm_config=llm_config,
                )

        # Run two calls concurrently
        results = await asyncio.gather(
            call_with_swap(gemini_provider, {"model": "gemini-2.5-flash"}),
            call_with_swap(ollama_provider, {"model": "qwen2.5:32b", "provider": "ollama"}),
        )

        # Each call used its own provider, no interference
        assert len(gemini_calls) == 1
        assert gemini_calls[0]["model"] == "gemini-2.5-flash"
        assert len(ollama_calls) == 1
        assert ollama_calls[0]["model"] == "qwen2.5:32b"
        # Default provider was not used
        assert default_calls == []

        # Both returned the correct response
        assert "gemini-2.5-flash" in results[0]
        assert "qwen2.5:32b" in results[1]

    @pytest.mark.asyncio
    async def test_concurrent_default_and_override_isolated(self):
        """A default call and an override call running concurrently
        use their respective providers without cross-contamination."""
        sup, _, default_calls = _make_supervisor()

        swap_calls: list[dict] = []

        class SlowProvider(ChatProvider):
            def __init__(self, name: str, calls: list):
                self.name = name
                self._calls = calls

            @property
            def model_name(self) -> str:
                return self.name

            async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
                await asyncio.sleep(0.05)
                self._calls.append({"model": self.name})
                return ChatResponse(content=[TextBlock(text=f"response-from-{self.name}")])

        swap_provider = SlowProvider("gemini-2.5-flash", swap_calls)

        # Make default provider also slow so they truly interleave
        class SlowDefaultProvider(ChatProvider):
            def __init__(self, calls: list):
                self._calls = calls

            @property
            def model_name(self) -> str:
                return "claude-sonnet-4-20250514"

            async def create_message(self, *, messages, system, tools=None, max_tokens=1024):
                await asyncio.sleep(0.05)
                self._calls.append({"model": "claude-sonnet-4-20250514"})
                return ChatResponse(content=[TextBlock(text="response-from-default")])

        sup._provider = SlowDefaultProvider(default_calls)

        async def call_default():
            # _resolve_call_provider returns None for no llm_config
            with patch.object(sup, "_resolve_call_provider", return_value=None):
                return await sup._chat_inner(text="default", user_name="tester")

        async def call_override():
            with patch.object(sup, "_resolve_call_provider", return_value=swap_provider):
                return await sup._chat_inner(
                    text="override",
                    user_name="tester",
                    llm_config={"model": "gemini-2.5-flash"},
                )

        await asyncio.gather(call_default(), call_override())

        # Each path used the correct provider
        assert len(default_calls) == 1
        assert default_calls[0]["model"] == "claude-sonnet-4-20250514"
        assert len(swap_calls) == 1
        assert swap_calls[0]["model"] == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_supervisor_provider_unchanged_after_concurrent_swaps(self):
        """After concurrent swap calls, self._provider still points to the
        original default provider -- swaps are truly ephemeral."""
        sup, original_provider, _ = _make_supervisor()

        providers = [
            RecordingProvider("gemini-2.5-flash"),
            RecordingProvider("claude-opus-4-20250514"),
        ]

        async def swap_call(idx):
            with patch.object(sup, "_resolve_call_provider", return_value=providers[idx]):
                return await sup._chat_inner(
                    text=f"call-{idx}",
                    user_name="tester",
                    llm_config={"model": providers[idx].name},
                )

        await asyncio.gather(swap_call(0), swap_call(1))

        # self._provider is still the original
        assert sup._provider is original_provider
        assert sup._provider.model_name == "claude-sonnet-4-20250514"
