from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from src.config import LLMConfig
from src.llm import LLMCallSpec, LLMClient
from src.llm.fake import FakeProvider
from src.llm.types import ChatResponse, TextBlock, ToolUseBlock
from src.llm_logger import LLMLogger


def _client(fake: FakeProvider, **cfg) -> LLMClient:
    return LLMClient.with_provider(fake, config=LLMConfig(**cfg))


async def test_complete_string_prompt_returns_text():
    fake = FakeProvider()
    fake.add_text("hello")
    resp = await _client(fake).complete("hi", system="sys")
    assert resp.text == "hello"
    assert resp.tool_calls == []
    call = fake.calls[0]
    assert call.messages == [{"role": "user", "content": "hi"}]
    assert call.system == "sys"
    assert call.max_tokens == 4096  # LLMConfig default


async def test_complete_joins_last_text_parts_and_exposes_tool_calls():
    fake = FakeProvider()
    fake.add_response(
        ChatResponse(content=[TextBlock(text="a"), ToolUseBlock(id="1", name="t", input={"x": 1}), TextBlock(text="b")])
    )
    resp = await _client(fake).complete([{"role": "user", "content": "q"}])
    assert resp.text == "a\nb"
    assert resp.tool_calls[0].name == "t" and resp.tool_calls[0].args == {"x": 1}


async def test_spec_max_tokens_passes_through():
    fake = FakeProvider()
    fake.add_text("x")
    await _client(fake).complete("q", spec=LLMCallSpec(max_tokens=17))
    assert fake.calls[0].max_tokens == 17


async def test_provider_cache_keyed_on_resolution():
    built = []

    def factory(**kw):
        built.append(kw)
        f = FakeProvider(model_name=kw["model"])
        f.add_text("ok")
        f.add_text("ok")
        return f

    client = LLMClient(LLMConfig(provider="anthropic", model="m1"), classes_loader=dict, provider_factory=factory)
    await client.complete("a")
    await client.complete("b")
    await client.complete("c", spec=LLMCallSpec(model="m2"))
    assert [b["model"] for b in built] == ["m1", "m2"]


async def test_logging_writes_llm_jsonl(tmp_path):
    logger = LLMLogger(base_dir=str(tmp_path), enabled=True)
    fake = FakeProvider()
    fake.add_text("logged")
    client = LLMClient.with_provider(fake, config=LLMConfig(), llm_logger=logger)
    await client.complete("q", spec=LLMCallSpec(caller="unit-test"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(str(tmp_path), today, "llm.jsonl")
    entry = json.loads(open(path).read().splitlines()[-1])
    assert entry["caller"] == "unit-test"
    assert entry["provider"] == "FakeProvider"
    assert entry["output"]["text_parts"] == ["logged"]


async def test_error_is_logged_and_reraised(tmp_path):
    logger = LLMLogger(base_dir=str(tmp_path), enabled=True)
    fake = FakeProvider()  # empty queue → RuntimeError
    client = LLMClient.with_provider(fake, config=LLMConfig(), llm_logger=logger)
    with pytest.raises(RuntimeError):
        await client.complete("q")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = json.loads(open(os.path.join(str(tmp_path), today, "llm.jsonl")).read().splitlines()[-1])
    assert "no scripted response" in entry["error"]


def test_is_configured_false_when_factory_fails():
    def boom(**kw):
        raise RuntimeError("no creds")

    client = LLMClient(LLMConfig(), classes_loader=dict, provider_factory=boom)
    assert client.is_configured() is False
    assert _client(FakeProvider()).is_configured() is True
