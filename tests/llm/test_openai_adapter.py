"""Behavioural tests for the OpenAI-format adapter (plan §intelligence 1-3).

The adapter is the boundary every OpenAI-compatible endpoint goes through,
so these tests assert the translated payloads directly rather than through
a provider.  Responses are built from minimal SDK-shaped objects — no
network, no ``openai`` import.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.llm.providers.adapters import openai_adapter
from src.llm.types import TextBlock, ToolUseBlock


def _response(*, content: str | None, tool_calls: list | None) -> SimpleNamespace:
    """Build a minimal object shaped like an OpenAI chat completion."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_convert_messages_preserves_tool_turn_and_result():
    messages = [
        {"role": "user", "content": "list the tasks"},
        {
            "role": "assistant",
            "content": [
                TextBlock(text="Looking that up."),
                ToolUseBlock(id="call_1", name="list_tasks", input={"project_id": "p"}),
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": '{"count": 2}'},
                {"type": "text", "text": "thanks"},
            ],
        },
    ]

    converted = openai_adapter.convert_messages(messages, system="You are helpful.")

    assert converted[0] == {"role": "system", "content": "You are helpful."}
    assert converted[1] == {"role": "user", "content": "list the tasks"}

    assistant = converted[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Looking that up."
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "list_tasks",
                "arguments": '{"project_id": "p"}',
            },
        }
    ]

    # The tool result keeps the original call ID so the endpoint can pair it
    # with the assistant turn above.
    assert converted[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"count": 2}',
    }
    assert converted[4] == {"role": "user", "content": "thanks"}


def test_parse_response_combines_text_and_json_tool_arguments():
    response = _response(
        content="Here you go.",
        tool_calls=[_tool_call("call_9", "create_task", '{"title": "ship it", "depth": 2}')],
    )

    parsed = openai_adapter.parse_response(response)

    assert parsed.content[0] == TextBlock(text="Here you go.")
    assert parsed.content[1] == ToolUseBlock(
        id="call_9", name="create_task", input={"title": "ship it", "depth": 2}
    )
    assert parsed.text_parts == ["Here you go."]
    assert parsed.has_tool_use


def test_parse_response_malformed_tool_arguments_do_not_abort_the_tool_loop():
    """INT-3: model-supplied argument JSON is untrusted input.

    A truncated or otherwise malformed ``arguments`` string must produce a
    controlled failure the caller can see, never an unhandled
    ``json.JSONDecodeError`` that kills the whole tool loop.  Valid calls in
    the same response must still be delivered.
    """
    response = _response(
        content=None,
        tool_calls=[
            _tool_call("call_bad", "create_task", '{"title": "ship it"'),
            _tool_call("call_ok", "list_tasks", '{"project_id": "p"}'),
        ],
    )

    parsed = openai_adapter.parse_response(response)

    # The good call survives untouched.
    assert parsed.tool_uses == [
        ToolUseBlock(id="call_ok", name="list_tasks", input={"project_id": "p"})
    ]
    # The bad call is reported as text instead of being executed with junk.
    error_text = "\n".join(parsed.text_parts)
    assert "create_task" in error_text
    assert "arguments" in error_text.lower()


def test_convert_tools_applies_empty_object_schema_default():
    converted = openai_adapter.convert_tools(
        [
            {"name": "ping", "description": "no arguments"},
            {
                "name": "echo",
                "description": "repeat",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        ]
    )

    assert converted[0] == {
        "type": "function",
        "function": {
            "name": "ping",
            "description": "no arguments",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    # A declared schema is passed through unchanged — the default only fills
    # in for tools that declare none.
    assert converted[1]["function"]["parameters"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }
