"""Logged-in CLI response parsing and budget accounting."""

from __future__ import annotations

import json

from src.llm.cli import _answer
from src.llm.types import TokenUsage


def test_codex_last_message_and_completed_usage(tmp_path) -> None:
    output = tmp_path / "answer.json"
    output.write_text('{"ok":true}')
    events = "\n".join((
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}),
    ))

    answer = _answer("codex", events, output)

    assert answer.text == '{"ok":true}'
    assert answer.usage == TokenUsage(100, 5, True)


def test_gemini_usage_includes_reasoning_tokens(tmp_path) -> None:
    response = {
        "response": '{"ok":true}',
        "stats": {"models": {"gemini": {"tokens": {
            "input": 10188, "prompt": 10188, "candidates": 5,
            "thoughts": 57, "total": 10250,
        }}}},
    }

    answer = _answer("gemini", json.dumps(response), tmp_path / "unused")

    assert answer.text == '{"ok":true}'
    assert answer.usage == TokenUsage(10188, 62, True)


def test_claude_usage_includes_cached_input(tmp_path) -> None:
    response = {
        "structured_output": {"ok": True},
        "usage": {"input_tokens": 2, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 50, "output_tokens": 52},
    }

    answer = _answer("claude", json.dumps(response), tmp_path / "unused")

    assert json.loads(answer.text) == {"ok": True}
    assert answer.usage == TokenUsage(1052, 52, True)
