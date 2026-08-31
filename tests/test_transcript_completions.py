"""Completion compatibility for native agent transcripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sessions.transcripts.claude import ClaudeTranscriptReader
from src.sessions.transcripts.codex import CodexTranscriptReader
from src.sessions.transcripts.base import TranscriptEntry


CODEX_UUID = "01a02602-d8b3-7ab1-9c8a-3718b27f1348"


def _jsonl(*records: dict) -> bytes:
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def _rollout(tmp_path: Path, body: bytes) -> Path:
    directory = tmp_path / ".codex" / "sessions" / "2026" / "08" / "30"
    directory.mkdir(parents=True)
    path = directory / f"rollout-2026-08-30T12-00-00-{CODEX_UUID}.jsonl"
    path.write_bytes(body)
    return path


@pytest.mark.asyncio
async def test_codex_emits_visible_final_answer_once_when_task_completes_later(tmp_path: Path):
    """Dropping response_item text loses the question that ends a turn."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            {
                "timestamp": "2026-08-30T12:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Checking files"}],
                },
            },
            {
                "timestamp": "2026-08-30T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Which test should I run?"}],
                },
            },
            {
                "timestamp": "2026-08-30T12:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "private instructions"}],
                },
            },
            {
                "timestamp": "2026-08-30T12:00:04Z",
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "private reasoning"},
            },
        ),
    )
    reader = CodexTranscriptReader(base_dir=tmp_path)

    first, offset = await reader.read_new(path, 0)
    assert [(entry.text, entry.turn_complete) for entry in first if entry.text] == [
        ("Checking files", False)
    ]

    with path.open("ab") as file:
        file.write(
            _jsonl(
                {
                    "timestamp": "2026-08-30T12:00:05Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"},
                }
            )
        )
    second, offset2 = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, offset)
    assert [(entry.text, entry.turn_complete) for entry in second if entry.text] == [
        ("Which test should I run?", True)
    ]
    assert second[0].parent_uuid == "turn-1"
    assert offset2 == path.stat().st_size
    assert not any("private" in entry.text for entry in first + second)


@pytest.mark.asyncio
async def test_codex_deduplicates_legacy_agent_message_and_response_item_text(tmp_path: Path):
    """Reading both Codex formats must not render one assistant message twice."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            {
                "timestamp": "2026-08-30T12:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "Checking files",
                },
            },
            {
                "timestamp": "2026-08-30T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Checking files"}],
                },
            },
        ),
    )

    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert [entry.text for entry in entries if entry.type == "assistant" and entry.text] == [
        "Checking files"
    ]


@pytest.mark.asyncio
async def test_codex_task_complete_without_final_text_emits_nothing(tmp_path: Path):
    """Completion metadata must not become an empty assistant message."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            },
        ),
    )

    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert entries == []


@pytest.mark.asyncio
async def test_claude_marks_text_end_turn_complete_but_not_tool_use(tmp_path: Path):
    """Treating a tool request as completion prematurely routes a question."""
    path = tmp_path / "claude.jsonl"
    path.write_bytes(
        _jsonl(
            {
                "type": "assistant",
                "uuid": "tool",
                "parentUuid": "user-1",
                "timestamp": "2026-08-30T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "exec_command"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "answer",
                "parentUuid": "user-1",
                "timestamp": "2026-08-30T12:00:01Z",
                "message": {
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Which test should I run?"}],
                },
            },
        )
    )

    entries, _ = await ClaudeTranscriptReader().read_new(path, 0)
    assert [(entry.text, entry.turn_complete) for entry in entries] == [
        ("[tool_use: exec_command]", False),
        ("Which test should I run?", True),
    ]


@pytest.mark.asyncio
async def test_codex_deduplicates_legacy_message_across_fresh_reader_polls(tmp_path: Path):
    """A watcher recreates readers, so deduplication cannot be instance-local."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "Checking files",
                },
            },
        ),
    )
    _entries, offset = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    with path.open("ab") as file:
        file.write(
            _jsonl(
                {
                    "timestamp": "2026-08-30T12:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Checking files"}],
                    },
                },
            )
        )

    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, offset)
    assert entries == []


@pytest.mark.asyncio
async def test_codex_reads_new_format_user_input_without_developer_frames(tmp_path: Path):
    """Ignoring response_item user messages prevents a human answer resolving a question."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "private instructions"}],
                },
            },
            {
                "timestamp": "2026-08-30T12:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-2",
                    "item": {
                        "type": "UserMessage",
                        "id": "item-user-1",
                        "content": [{"type": "text", "text": "Please continue"}],
                    },
                },
            },
        ),
    )

    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert [(entry.type, entry.text) for entry in entries] == [("user", "Please continue")]


@pytest.mark.asyncio
async def test_codex_reused_path_rereads_a_shorter_new_transcript(tmp_path: Path):
    """A stale offset must not attach an old final answer to a reused path."""
    old_path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "x" * 1_000}],
                },
            },
        ),
    )
    _entries, old_offset = await CodexTranscriptReader(base_dir=tmp_path).read_new(old_path, 0)
    old_path.write_bytes(
        _jsonl(
            {
                "timestamp": "2026-08-30T12:01:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-2"},
            },
            {
                "timestamp": "2026-08-30T12:01:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Which test should I run?"}],
                },
            },
            {
                "timestamp": "2026-08-30T12:01:02Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-2"},
            },
        )
    )

    entries, new_offset = await CodexTranscriptReader(base_dir=tmp_path).read_new(
        old_path, old_offset
    )
    assert [(entry.text, entry.turn_complete) for entry in entries] == [
        ("Which test should I run?", True)
    ]
    assert new_offset == old_path.stat().st_size


@pytest.mark.asyncio
async def test_codex_mixed_final_encodings_emit_only_completed_response(tmp_path: Path):
    """Legacy and native records for one final must not render it twice."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            {
                "timestamp": "2026-08-30T12:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "Which test should I run?",
                },
            },
            {
                "timestamp": "2026-08-30T12:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Which test should I run?"}],
                },
            },
            {
                "timestamp": "2026-08-30T12:00:03Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            },
        ),
    )

    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert [(entry.text, entry.turn_complete) for entry in entries] == [
        ("Which test should I run?", True),
    ]


@pytest.mark.asyncio
async def test_codex_task_complete_uses_visible_last_agent_message_fallback(tmp_path: Path):
    """Some native rollouts contain the final text only on task_complete."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "Which test should I run?",
                },
            },
        ),
    )

    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert [(entry.text, entry.turn_complete) for entry in entries] == [
        ("Which test should I run?", True),
    ]


def test_completed_turn_before_usage_entry_is_idle():
    """Trailing token accounting cannot make a completed turn look busy."""
    now = __import__("time").time()
    reader = ClaudeTranscriptReader()
    tail = [
        TranscriptEntry(
            "answer", "turn", "assistant", "Question?", None, None, now, turn_complete=True
        ),
        TranscriptEntry("usage", "turn", "assistant", "", None, {"output_tokens": 1}, now),
    ]
    assert reader.infer_activity(tail) == "idle"


@pytest.mark.asyncio
async def test_codex_cold_replay_scans_prefix_once_for_many_completions(
    tmp_path: Path, monkeypatch
):
    """Cold replay must not reread the whole transcript once per completion."""
    path = _rollout(
        tmp_path,
        _jsonl(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "old"},
            },
        ),
    )
    _entries, offset = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    with path.open("ab") as file:
        file.write(
            _jsonl(
                {
                    "timestamp": "2026-08-30T12:01:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-08-30T12:01:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "First?"}],
                    },
                },
                {
                    "timestamp": "2026-08-30T12:01:02Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-08-30T12:02:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2"},
                },
                {
                    "timestamp": "2026-08-30T12:02:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Second?"}],
                    },
                },
                {
                    "timestamp": "2026-08-30T12:02:02Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-2"},
                },
            )
        )
    opens = 0
    original_open = Path.open

    def count_open(self, *args, **kwargs):
        nonlocal opens
        if self == path:
            opens += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_open)
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, offset)
    assert [entry.text for entry in entries] == ["First?", "Second?"]
    assert opens == 2  # one incremental read plus one prefix reconstruction
