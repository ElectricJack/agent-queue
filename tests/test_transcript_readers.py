"""Transcript reader tests (S3, Task B1)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.sessions.transcripts import resolve_reader
from src.sessions.transcripts.base import TranscriptEntry, TranscriptReader
from src.sessions.transcripts.claude import ClaudeTranscriptReader


FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude"


def _slug(work_dir: str) -> str:
    return work_dir.replace("/", "-").replace(".", "-")


def test_resolve_reader_returns_claude_for_claude_harness():
    r = resolve_reader("claude")
    assert isinstance(r, ClaudeTranscriptReader)


def test_resolve_reader_returns_none_for_unknown():
    assert resolve_reader("nonesuch") is None


def test_resolve_reader_forwards_base_dir(tmp_path: Path):
    r = resolve_reader("claude", base_dir=tmp_path)
    assert r is not None
    assert r.base_dir == tmp_path


def test_slug_resolution_with_dots_and_slashes(tmp_path: Path):
    work_dir = "/home/jkern/dev/agent.queue"
    slug = _slug(work_dir)  # "-home-jkern-dev-agent-queue"
    proj = tmp_path / ".claude" / "projects" / slug
    proj.mkdir(parents=True)
    (proj / "abc.jsonl").write_text("")
    r = ClaudeTranscriptReader(base_dir=tmp_path)
    resolved = r.resolve_path(work_dir, "abc")
    assert resolved == proj / "abc.jsonl"


def test_resolve_path_uses_session_key_when_known(tmp_path: Path):
    slug = _slug("/w")
    proj = tmp_path / ".claude" / "projects" / slug
    proj.mkdir(parents=True)
    older = proj / "a.jsonl"
    newer = proj / "b.jsonl"
    older.write_text("")
    time.sleep(0.02)
    newer.write_text("")
    r = ClaudeTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/w", "a") == older


def test_resolve_path_falls_back_to_newest_mtime(tmp_path: Path):
    slug = _slug("/w2")
    proj = tmp_path / ".claude" / "projects" / slug
    proj.mkdir(parents=True)
    older = proj / "a.jsonl"
    newer = proj / "b.jsonl"
    older.write_text("")
    time.sleep(0.02)
    newer.write_text("")
    r = ClaudeTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/w2", None) == newer


def test_resolve_path_missing_dir_returns_none(tmp_path: Path):
    r = ClaudeTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/no/such/dir", None) is None


def test_resolve_path_empty_dir_returns_none(tmp_path: Path):
    slug = _slug("/empty")
    (tmp_path / ".claude" / "projects" / slug).mkdir(parents=True)
    r = ClaudeTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/empty", None) is None


@pytest.mark.asyncio
async def test_read_new_parses_fixture(tmp_path: Path):
    src = FIXTURES / "basic.jsonl"
    dst = tmp_path / "t.jsonl"
    dst.write_bytes(src.read_bytes())

    r = ClaudeTranscriptReader()
    entries, offset = await r.read_new(dst, 0)
    assert offset == dst.stat().st_size
    # Summary line skipped (type=summary)
    types = [e.type for e in entries]
    assert "user" in types
    assert "assistant" in types
    # Assistant with usage present
    with_usage = [e for e in entries if e.type == "assistant" and e.usage]
    assert with_usage, "at least one assistant entry has usage"
    first_use = with_usage[0]
    assert first_use.usage["output_tokens"] == 25
    assert first_use.model == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_read_new_incremental_across_appends(tmp_path: Path):
    dst = tmp_path / "t.jsonl"
    line1 = json.dumps({
        "type": "user", "uuid": "u1", "parentUuid": None,
        "timestamp": "2026-08-20T12:00:00.000Z",
        "message": {"role": "user", "content": "one"},
    })
    line2 = json.dumps({
        "type": "assistant", "uuid": "a1", "parentUuid": "u1",
        "timestamp": "2026-08-20T12:00:01.000Z",
        "message": {"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": "two"}],
                     "usage": {"input_tokens": 1, "output_tokens": 2}},
    })
    dst.write_text(line1 + "\n")

    r = ClaudeTranscriptReader()
    entries1, off1 = await r.read_new(dst, 0)
    assert len(entries1) == 1
    assert entries1[0].uuid == "u1"

    # Append with a partial trailing line
    partial_prefix = ('{"type":"user","uuid":"u2","parentUuid":"a1",'
                      '"timestamp":"2026-08-20T12:00:02.000Z",'
                      '"message":{"role":"user","content":"thr')
    with dst.open("a") as f:
        f.write(line2 + "\n")
        f.write(partial_prefix)  # unterminated
    entries2, off2 = await r.read_new(dst, off1)
    uuids = [e.uuid for e in entries2]
    assert "a1" in uuids
    assert off2 < dst.stat().st_size, "partial trailing line left unconsumed"

    # Complete the trailing line; next read picks it up, no duplicates
    with dst.open("a") as f:
        f.write('ee"}}\n')
    entries3, off3 = await r.read_new(dst, off2)
    assert [e.uuid for e in entries3] == ["u2"]
    assert off3 == dst.stat().st_size


def test_infer_activity_in_turn_when_recent_assistant():
    now = time.time()
    tail = [
        TranscriptEntry(uuid="a", parent_uuid=None, type="assistant",
                        text="hi", model="m", usage={"output_tokens": 1},
                        ts=now - 1.0),
    ]
    r = ClaudeTranscriptReader()
    assert r.infer_activity(tail) == "in-turn"


def test_infer_activity_idle_after_user_turn():
    now = time.time()
    tail = [
        TranscriptEntry(uuid="a", parent_uuid=None, type="assistant",
                        text="hi", model="m", usage={"output_tokens": 1},
                        ts=now - 300.0),
        TranscriptEntry(uuid="u", parent_uuid="a", type="user",
                        text="ok", model=None, usage=None,
                        ts=now - 299.0),
    ]
    r = ClaudeTranscriptReader()
    assert r.infer_activity(tail) == "idle"


def test_infer_activity_empty_tail_is_idle():
    r = ClaudeTranscriptReader()
    assert r.infer_activity([]) == "idle"


def test_transcript_reader_abc_declares_harness():
    assert TranscriptReader.__abstractmethods__  # class is abstract
    assert ClaudeTranscriptReader.harness == "claude"
