"""Provider package layout, FakeProvider, and the factory (spec §3)."""

from __future__ import annotations

import pytest

from src.llm.fake import FakeProvider
from src.llm.providers import create_provider
from src.llm.providers.base import LLMProvider
from src.llm.types import ChatResponse, TextBlock, ToolUseBlock


class TestFakeProvider:
    async def test_fifo_text_and_records_calls(self):
        fake = FakeProvider()
        fake.add_text("one")
        fake.add_text("two")
        r1 = await fake.create_message(messages=[{"role": "user", "content": "a"}], system="s")
        r2 = await fake.create_message(messages=[{"role": "user", "content": "b"}], system="s")
        assert r1.text_parts == ["one"] and r2.text_parts == ["two"]
        assert [c.messages[0]["content"] for c in fake.calls] == ["a", "b"]
        assert fake.model_name == "fake"
        assert isinstance(fake, LLMProvider)

    async def test_tool_call_helper(self):
        fake = FakeProvider()
        fake.add_tool_call("list_tasks", {"project_id": "p"})
        resp = await fake.create_message(messages=[], system="")
        assert resp.has_tool_use
        assert resp.tool_uses[0].name == "list_tasks"
        assert resp.tool_uses[0].input == {"project_id": "p"}

    async def test_exhausted_queue_raises(self):
        fake = FakeProvider()
        with pytest.raises(RuntimeError, match="no scripted response"):
            await fake.create_message(messages=[], system="")


class TestFactory:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="unknown llm provider"):
            create_provider(provider="nope", model="m", base_url="", api_key="", extras={})

    def test_openai_uses_base_url(self):
        pytest.importorskip("openai")
        p = create_provider(
            provider="openai", model="qwen3", base_url="http://localhost:11434/v1",
            api_key="", extras={},
        )
        assert p.model_name == "qwen3"
        assert type(p).__name__ == "OpenAIProvider"

    def test_google_class_name(self):
        pytest.importorskip("google.genai")
        p = create_provider(provider="google", model="gemini-2.5-flash", base_url="", api_key="k", extras={})
        assert type(p).__name__ == "GoogleProvider"
        assert p.model_name == "gemini-2.5-flash"

    def test_anthropic_default_model(self, monkeypatch):
        pytest.importorskip("anthropic")
        for var in ("GOOGLE_CLOUD_PROJECT", "ANTHROPIC_VERTEX_PROJECT_ID", "AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        p = create_provider(provider="anthropic", model="", base_url="", api_key="sk-test", extras={})
        assert type(p).__name__ == "AnthropicProvider"
        assert p.model_name == "claude-sonnet-5"


def test_types_reexported():
    assert ChatResponse(content=[TextBlock(text="x"), ToolUseBlock(id="1", name="t", input={})]).has_tool_use
