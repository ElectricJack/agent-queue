from __future__ import annotations

import asyncio

from src.config import LLMConfig
from src.llm import LLMClient
from src.llm.fake import FakeProvider

TOOLS = [
    {"name": "list_tasks", "description": "list", "input_schema": {"type": "object", "properties": {}}},
    {"name": "boom", "description": "raises", "input_schema": {"type": "object", "properties": {}}},
]


def _client(fake):
    return LLMClient.with_provider(fake, config=LLMConfig())


async def _exec(name, args):
    if name == "boom":
        raise ValueError("kaboom")
    return {"success": True, "tool": name, "args": args}


async def test_no_tool_calls_returns_text_in_one_turn():
    fake = FakeProvider()
    fake.add_text("done")
    r = await _client(fake).run_tools("go", TOOLS, _exec, system="s")
    assert (r.text, r.turns, r.stopped_by) == ("done", 1, "done")
    assert r.transcript[0] == {"role": "user", "content": "go"}
    assert r.transcript[-1] == {"role": "assistant", "content": "done"}
    assert fake.calls[0].tools == TOOLS


async def test_tool_call_then_text():
    fake = FakeProvider()
    fake.add_tool_call("list_tasks", {"project_id": "p"})
    fake.add_text("finished")
    events = []

    async def progress(kind, detail):
        events.append((kind, detail))

    r = await _client(fake).run_tools("go", TOOLS, _exec, on_progress=progress)
    assert r.text == "finished" and r.turns == 2
    assert r.tool_calls_made == ["list_tasks"]
    # second request carries assistant tool_use + user tool_result
    second = fake.calls[1].messages
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "user"
    result_block = second[-1]["content"][0]
    assert result_block["type"] == "tool_result"
    assert '"tool": "list_tasks"' in result_block["content"]
    assert [k for k, _ in events] == ["thinking", "tool_use", "thinking", "responding"]


async def test_executor_exception_becomes_error_result_not_abort():
    fake = FakeProvider()
    fake.add_tool_call("boom")
    fake.add_text("recovered")
    r = await _client(fake).run_tools("go", TOOLS, _exec)
    assert r.text == "recovered"
    block = fake.calls[1].messages[-1]["content"][0]
    assert '"success": false' in block["content"] and "kaboom" in block["content"]


async def test_unknown_tool_is_rejected_as_error_result():
    fake = FakeProvider()
    fake.add_tool_call("not_offered")
    fake.add_text("ok")
    r = await _client(fake).run_tools("go", TOOLS, _exec)
    assert r.text == "ok"
    block = fake.calls[1].messages[-1]["content"][0]
    assert "not available" in block["content"]


async def test_max_turns_stops_loop():
    fake = FakeProvider()
    for _ in range(3):
        fake.add_tool_call("list_tasks")
    r = await _client(fake).run_tools("go", TOOLS, _exec, max_turns=2)
    assert r.stopped_by == "max_turns" and r.turns == 2
    assert len(fake.calls) == 2


async def test_cancel_event_stops_before_next_call():
    fake = FakeProvider()
    fake.add_tool_call("list_tasks")
    fake.add_text("never")
    cancel = asyncio.Event()

    async def exec_and_cancel(name, args):
        cancel.set()
        return {"success": True}

    r = await _client(fake).run_tools("go", TOOLS, exec_and_cancel, cancel_event=cancel)
    assert r.stopped_by == "cancelled" and r.text == ""
    assert len(fake.calls) == 1


async def test_multi_tool_turn_executes_all_in_order():
    fake = FakeProvider()
    fake.add_tool_calls([("list_tasks", {"a": 1}), ("list_tasks", {"a": 2})])
    fake.add_text("ok")
    seen = []

    async def ex(name, args):
        seen.append(args["a"])
        return {}

    await _client(fake).run_tools("go", TOOLS, ex)
    assert seen == [1, 2]
