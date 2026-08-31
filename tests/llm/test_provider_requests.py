"""Request-shaping and readiness tests for the three chat providers
(plan §intelligence 7-11).

Every vendor SDK is replaced with a recording double, so these tests assert
the exact call the provider *would* make — credentials, thinking budgets,
local keep-alive options, tool translation — without a network round trip.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from src.llm.providers.anthropic import AnthropicProvider
from src.llm.providers.openai import OpenAIProvider
from tests.llm import fake_genai


_ANTHROPIC_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_API_KEY",
    "GOOGLE_CLOUD_LOCATION",
    "CLOUD_ML_REGION",
)


def _clear_anthropic_env(monkeypatch) -> None:
    for var in _ANTHROPIC_ENV:
        monkeypatch.delenv(var, raising=False)


class _RecordingAnthropicClient:
    """Stands in for ``anthropic.AsyncAnthropic`` and its Vertex/Bedrock kin."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self.response = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="thought about it"),
                SimpleNamespace(
                    type="tool_use", id="tu_1", name="list_tasks", input={"project_id": "p"}
                ),
            ]
        )

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _install_anthropic_doubles(monkeypatch) -> dict[str, list]:
    """Replace all four Anthropic client classes with recorders."""
    import anthropic

    built: dict[str, list] = {"direct": [], "vertex": [], "bedrock": []}

    def _factory(kind):
        def _make(**kwargs):
            client = _RecordingAnthropicClient(**kwargs)
            built[kind].append(client)
            return client

        return _make

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _factory("direct"))
    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", _factory("vertex"))
    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrock", _factory("bedrock"))
    return built


async def test_anthropic_thinking_increases_max_tokens_and_forwards_tools(monkeypatch):
    pytest.importorskip("anthropic")
    _clear_anthropic_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    built = _install_anthropic_doubles(monkeypatch)

    provider = AnthropicProvider(model="claude-opus-5", thinking_budget=4096)
    assert provider.is_configured
    tools = [{"name": "list_tasks", "input_schema": {"type": "object", "properties": {}}}]

    response = await provider.create_message(
        messages=[{"role": "user", "content": "hi"}],
        system="be brief",
        tools=tools,
        max_tokens=1024,
    )

    (call,) = built["direct"][0].calls
    assert call["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    # The thinking budget is spent from max_tokens, so the visible response
    # budget has to be raised above it.
    assert call["max_tokens"] == 4096 + 1024
    assert call["tools"] == tools
    assert call["system"] == "be brief"
    assert call["model"] == "claude-opus-5"

    assert response.text_parts == ["thought about it"]
    assert response.tool_uses[0].name == "list_tasks"
    assert response.tool_uses[0].input == {"project_id": "p"}


async def test_anthropic_thinking_disabled_leaves_max_tokens_alone(monkeypatch):
    """Positive control for the arithmetic above."""
    pytest.importorskip("anthropic")
    _clear_anthropic_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    built = _install_anthropic_doubles(monkeypatch)

    provider = AnthropicProvider(model="claude-opus-5", thinking_budget=0)
    await provider.create_message(messages=[], system="s", tools=None, max_tokens=1024)

    (call,) = built["direct"][0].calls
    assert "thinking" not in call
    assert "tools" not in call
    assert call["max_tokens"] == 1024


def test_anthropic_authentication_prefers_vertex_over_other_credentials(monkeypatch):
    pytest.importorskip("anthropic")
    _clear_anthropic_env(monkeypatch)
    # Every other credential source is also present; Vertex must still win.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    built = _install_anthropic_doubles(monkeypatch)
    # Guard against the OAuth fall-through reading a real credentials file.
    monkeypatch.setattr(
        "src.llm.providers.anthropic._load_claude_oauth_token", lambda: "oauth-token"
    )

    provider = AnthropicProvider(model="", api_key="sk-explicit")

    assert len(built["vertex"]) == 1
    assert built["bedrock"] == [] and built["direct"] == []
    assert built["vertex"][0].init_kwargs == {"project_id": "proj-1", "region": "europe-west4"}
    assert provider.is_configured
    assert provider.model_name == "claude-sonnet-5"


def test_anthropic_falls_through_to_bedrock_when_vertex_absent(monkeypatch):
    """Positive control: the fall-through really is ordered, not hard-wired."""
    pytest.importorskip("anthropic")
    _clear_anthropic_env(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    built = _install_anthropic_doubles(monkeypatch)

    AnthropicProvider(model="")

    assert len(built["bedrock"]) == 1
    assert built["vertex"] == [] and built["direct"] == []


class _RecordingOpenAIClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _install_openai_double(monkeypatch) -> list[_RecordingOpenAIClient]:
    import openai

    built: list[_RecordingOpenAIClient] = []

    def _make(**kwargs):
        client = _RecordingOpenAIClient(**kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _make)
    return built


async def test_openai_local_request_forwards_keep_alive_context_and_reasoning(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    built = _install_openai_double(monkeypatch)

    provider = OpenAIProvider(
        model="qwen3.5:35b",
        base_url="http://localhost:11434/v1",
        keep_alive="30m",
        num_ctx=32768,
        reasoning_effort="high",
    )
    response = await provider.create_message(
        messages=[{"role": "user", "content": "hello"}],
        system="be brief",
        tools=[{"name": "ping", "description": "no args"}],
        max_tokens=512,
    )

    client = built[0]
    assert client.init_kwargs["base_url"] == "http://localhost:11434/v1"
    assert client.init_kwargs["api_key"] == "ollama"

    (call,) = client.calls
    assert call["extra_body"] == {"keep_alive": "30m", "options": {"num_ctx": 32768}}
    assert call["reasoning_effort"] == "high"
    assert call["max_tokens"] == 512
    assert call["messages"][0] == {"role": "system", "content": "be brief"}
    assert call["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "no args",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert response.text_parts == ["done"]


async def test_openai_hosted_request_omits_local_only_options(monkeypatch):
    """Positive control: ``extra_body`` is local-endpoint-only."""
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-hosted")
    built = _install_openai_double(monkeypatch)

    provider = OpenAIProvider(model="", base_url="https://api.openai.com/v1", num_ctx=32768)
    await provider.create_message(messages=[], system="s", tools=None, max_tokens=256)

    (call,) = built[0].calls
    assert "extra_body" not in call
    assert "reasoning_effort" not in call
    assert provider.model_name == "gpt-5"


async def test_openai_model_probe_uses_recent_success_then_fails_open(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _install_openai_double(monkeypatch)

    provider = OpenAIProvider(
        model="qwen3.5:35b", base_url="http://localhost:11434/v1", keep_alive="1h"
    )

    def _explode(*args, **kwargs):
        raise AssertionError("probe must not touch the network inside the keep-alive window")

    monkeypatch.setattr("urllib.request.urlopen", _explode)

    # Fast path: a recent successful call means the model is still resident.
    provider._last_request_at = time.monotonic()
    assert await provider.is_model_loaded() is True

    # Outside the window the probe runs — and fails open so a broken probe
    # never blocks a caller.
    provider._last_request_at = 0.0

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert await provider.is_model_loaded() is True


class _RecordingGenaiClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs):
        self.calls.append(kwargs)
        part = SimpleNamespace(text="gemini says hi", function_call=None)
        content = SimpleNamespace(parts=[part])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


async def test_google_request_adds_thinking_budget_and_converts_tools(monkeypatch):
    built: list[_RecordingGenaiClient] = []

    def _make(**kwargs):
        client = _RecordingGenaiClient(**kwargs)
        built.append(client)
        return client

    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    fake_genai.install(monkeypatch, client_factory=_make)

    from src.llm.providers.google import GoogleProvider

    provider = GoogleProvider(model="gemini-2.5-pro", api_key="k", thinking_budget=8192)
    response = await provider.create_message(
        messages=[{"role": "user", "content": "hello"}],
        system="be brief",
        tools=[
            {
                "name": "ping",
                "description": "no args",
                "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        ],
        max_tokens=1024,
    )

    assert built[0].init_kwargs == {"api_key": "k"}
    (call,) = built[0].calls
    assert call["model"] == "gemini-2.5-pro"

    config = call["config"]
    assert config.system_instruction == "be brief"
    # Gemini charges thinking tokens against max_output_tokens, so the budget
    # is added on top of the caller's response budget.
    assert config.max_output_tokens == 1024 + 8192
    assert config.thinking_config.thinking_budget == 8192
    assert config.tools[0].function_declarations[0].name == "ping"
    assert config.tools[0].function_declarations[0].parameters.type == "OBJECT"

    assert call["contents"][0].role == "user"
    assert call["contents"][0].parts[0].text == "hello"

    assert response.text_parts == ["gemini says hi"]
