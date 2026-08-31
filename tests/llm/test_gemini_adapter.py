"""Behavioural tests for the Gemini adapter (plan §intelligence 4-6).

``google.genai`` is imported lazily inside every adapter function, so these
tests install the deterministic shim from ``tests/llm/fake_genai.py`` and
assert on the objects the adapter constructed.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.llm.providers.adapters import gemini_adapter
from src.llm.types import TextBlock, ToolUseBlock
from tests.llm import fake_genai


def test_convert_messages_resolves_tool_result_to_original_function_name(monkeypatch):
    fake_genai.install(monkeypatch)

    messages = [
        {"role": "user", "content": "how many tasks?"},
        {
            "role": "assistant",
            "content": [
                TextBlock(text="Checking."),
                ToolUseBlock(id="call_42", name="list_tasks", input={"project_id": "p"}),
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_42", "content": '{"count": 3}'}
            ],
        },
    ]

    contents = gemini_adapter.convert_messages(messages)

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[0].parts[0].text == "how many tasks?"

    model_parts = contents[1].parts
    assert model_parts[0].text == "Checking."
    assert model_parts[1].function_call.name == "list_tasks"
    assert model_parts[1].function_call.args == {"project_id": "p"}

    # Gemini keys function responses by function name, not by the opaque
    # call ID the internal format uses.
    response_part = contents[2].parts[0]
    assert response_part.function_response.name == "list_tasks"
    assert response_part.function_response.response == {"count": 3}


def test_convert_messages_wraps_unresolvable_tool_result_text(monkeypatch):
    """A tool result with no matching assistant call falls back to its ID,
    and non-JSON payloads are wrapped rather than dropped."""
    fake_genai.install(monkeypatch)

    contents = gemini_adapter.convert_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "plain text"}
                ],
            }
        ]
    )

    part = contents[0].parts[0]
    assert part.function_response.name == "orphan"
    assert part.function_response.response == {"result": "plain text"}


def test_convert_schema_handles_nested_array_and_nullable_type(monkeypatch):
    fake_genai.install(monkeypatch)

    tools = gemini_adapter.convert_tools(
        [
            {
                "name": "create_tasks",
                "description": "make some tasks",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "titles": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": ["string", "null"], "description": "optional"},
                        "mode": {"type": "string", "enum": ["fast", None, "slow"]},
                        "nested": {
                            "type": "object",
                            "properties": {
                                "rows": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {"id": {"type": "integer"}},
                                        "required": ["id"],
                                    },
                                }
                            },
                        },
                    },
                    "required": ["titles"],
                },
            }
        ]
    )

    assert len(tools) == 1
    declaration = tools[0].function_declarations[0]
    assert declaration.name == "create_tasks"

    params = declaration.parameters
    assert params.type == "OBJECT"
    assert params.kwargs["required"] == ["titles"]

    titles = params.properties["titles"]
    assert titles.type == "ARRAY"
    assert titles.items.type == "STRING"

    # Union types are collapsed to the first non-null member; Gemini has no
    # nullable type.
    assert params.properties["notes"].type == "STRING"
    assert params.properties["notes"].kwargs["description"] == "optional"

    # ``None`` is not a legal enum member on the wire.
    assert params.properties["mode"].kwargs["enum"] == ["fast", "slow"]

    rows = params.properties["nested"].properties["rows"]
    assert rows.type == "ARRAY"
    assert rows.items.properties["id"].type == "INTEGER"
    assert rows.items.kwargs["required"] == ["id"]


def test_convert_tools_omits_parameters_for_schemaless_tool(monkeypatch):
    fake_genai.install(monkeypatch)

    tools = gemini_adapter.convert_tools([{"name": "ping", "description": "no args"}])

    assert tools[0].function_declarations[0].parameters is None


def _gemini_response(parts) -> SimpleNamespace:
    content = SimpleNamespace(parts=parts) if parts is not None else None
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def test_parse_response_returns_empty_text_for_no_candidates_and_text_for_candidates():
    """Negative case with its positive control (review R6).

    A ``parse_response`` gutted to always return empty text cannot pass the
    second half of this test.
    """
    empty = gemini_adapter.parse_response(SimpleNamespace(candidates=[]))
    assert empty.content == [TextBlock(text="")]
    assert empty.text_parts == [""]
    assert not empty.has_tool_use

    # Same for a candidate that carries no parts at all.
    assert gemini_adapter.parse_response(_gemini_response(None)).content == [TextBlock(text="")]

    populated = gemini_adapter.parse_response(
        _gemini_response(
            [
                SimpleNamespace(text="here is the answer", function_call=None),
                SimpleNamespace(
                    text=None,
                    function_call=SimpleNamespace(name="list_tasks", args={"project_id": "p"}),
                ),
            ]
        )
    )

    assert populated.text_parts == ["here is the answer"]
    assert populated.has_tool_use
    tool_use = populated.tool_uses[0]
    assert (tool_use.name, tool_use.input) == ("list_tasks", {"project_id": "p"})
    assert tool_use.id
