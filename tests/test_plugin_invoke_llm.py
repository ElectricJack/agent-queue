"""Orchestrator._plugin_invoke_llm routes to LLMClient.complete / run_tools."""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.config import AppConfig, DiscordConfig
from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.orchestrator import Orchestrator


def _orch(tmp_path, fake):
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "w"), database_path=str(tmp_path / "t.db"),
                    data_dir=str(tmp_path / "d"))
    o = Orchestrator(cfg)
    o.llm = LLMClient.with_provider(fake)
    return o


async def test_plain_prompt_uses_complete(tmp_path):
    fake = FakeProvider()
    fake.add_text("4")
    o = _orch(tmp_path, fake)
    assert await o._plugin_invoke_llm("2+2?", "calc") == "4"
    assert fake.calls[0].tools is None
    assert "plugin:calc" in fake.calls[0].system


async def test_tools_use_run_tools_with_handler_execute(tmp_path):
    fake = FakeProvider()
    fake.add_tool_call("list_tasks", {"x": 1})
    fake.add_text("done")
    o = _orch(tmp_path, fake)
    handler = AsyncMock()
    handler.execute = AsyncMock(return_value={"success": True})
    o._command_handler = handler
    tools = [{"name": "list_tasks", "input_schema": {"type": "object", "properties": {}}}]
    assert await o._plugin_invoke_llm("go", "p", tools=tools) == "done"
    handler.execute.assert_awaited_once_with("list_tasks", {"x": 1})
