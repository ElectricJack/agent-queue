"""Transcript reader tests (S3, Task B1)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.sessions.transcripts import resolve_reader
from src.sessions.transcripts.base import TranscriptEntry, TranscriptReader
from src.sessions.transcripts.claude import ClaudeTranscriptReader
from src.sessions.transcripts.codex import CodexTranscriptReader


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


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

CODEX_FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "codex"
CODEX_UUID = "01a02602-d8b3-7ab1-9c8a-3718b27f1348"


def _codex_rollout(base: Path, *, day="2026/08/21", uuid=CODEX_UUID, body=None) -> Path:
    """Write a rollout file into the date-partitioned tree Codex uses."""
    day_dir = base / ".codex" / "sessions" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-21T13-28-35-{uuid}.jsonl"
    path.write_bytes(
        body if body is not None else (CODEX_FIXTURES / "basic.jsonl").read_bytes()
    )
    return path


def test_resolve_reader_returns_codex_for_codex_harness():
    assert isinstance(resolve_reader("codex"), CodexTranscriptReader)


def test_codex_resolves_by_cwd_because_the_tree_is_keyed_by_date(tmp_path: Path):
    """There is no slug to compute — cwd comes out of the file's first line."""
    path = _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/tmp/wd", None) == path


def test_codex_trailing_slash_does_not_defeat_the_cwd_match(tmp_path: Path):
    path = _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/tmp/wd/", None) == path


def test_codex_unknown_cwd_is_missing_not_wrong(tmp_path: Path):
    """Returning the newest file regardless of cwd would attribute one
    session's transcript to another."""
    _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/somewhere/else", None) is None


def test_codex_session_key_short_circuits_the_scan(tmp_path: Path):
    path = _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    # cwd deliberately wrong: the key alone must resolve it.
    assert r.resolve_path("/not/the/cwd", CODEX_UUID) == path


def test_codex_unmatched_key_falls_back_to_cwd(tmp_path: Path):
    """A key can belong to a rolled-off file; a cwd match still beats
    reporting the transcript missing."""
    path = _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    assert r.resolve_path("/tmp/wd", "ffffffff-0000-0000-0000-000000000000") == path


def test_codex_missing_root_returns_none(tmp_path: Path):
    assert CodexTranscriptReader(base_dir=tmp_path).resolve_path("/tmp/wd", None) is None


def test_codex_discovers_the_conversation_id_it_could_not_pin(tmp_path: Path):
    """The whole point: codex picks its own UUID, so we read it back."""
    path = _codex_rollout(tmp_path)
    assert CodexTranscriptReader(base_dir=tmp_path).discover_session_key(path) == CODEX_UUID


@pytest.mark.asyncio
async def test_codex_read_new_takes_text_from_events_and_tools_from_items(tmp_path: Path):
    """Both channels record the same turns; mixing them double-counts."""
    path = _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    entries, offset = await r.read_new(path, 0)
    assert offset == path.stat().st_size

    texts = [e.text for e in entries if e.type in ("user", "assistant") and e.text]
    assert texts.count("do the thing") == 1, "response_item message duplicates event_msg"
    assert texts.count("On it.") == 1
    # The developer frame (system prompt) and encrypted reasoning are not
    # conversation and must never reach the stream.
    assert not any("permissions instructions" in (e.text or "") for e in entries)
    assert not any("gAAAAA" in (e.text or "") for e in entries)
    # Tools only exist in response_item.
    assert [e.text for e in entries if e.type == "tool_use"] == ["[tool_use: exec_command]"]
    assert [e.type for e in entries if e.type == "tool_result"] == ["tool_result"]


@pytest.mark.asyncio
async def test_codex_token_count_bills_the_turn_not_the_session(tmp_path: Path):
    """``last_token_usage``, never the cumulative total — the watcher charges
    per entry, so cumulative figures re-bill the whole session every turn."""
    path = _codex_rollout(tmp_path)
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    billed = [e for e in entries if e.usage]
    assert len(billed) == 1
    # input_tokens has the cached share removed: the ledger prices cached
    # reads separately, so leaving it in would charge them at the full rate.
    assert billed[0].usage == {
        "input_tokens": 13972 - 6528,
        "cache_read_input_tokens": 6528,
        "output_tokens": 89,
    }
    # A token_count with no info is not a charge.
    assert billed[0].type == "assistant" and billed[0].text == ""


@pytest.mark.asyncio
async def test_codex_uuids_are_stable_across_a_reread(tmp_path: Path):
    """Codex lines carry no id; the watcher's double-billing guard keys on
    ours, so the same line must produce the same uuid every time."""
    path = _codex_rollout(tmp_path)
    r = CodexTranscriptReader(base_dir=tmp_path)
    first, _ = await r.read_new(path, 0)
    again, _ = await r.read_new(path, 0)
    assert [e.uuid for e in first] == [e.uuid for e in again]
    assert len({e.uuid for e in first}) == len(first), "uuids unique within a file"


@pytest.mark.asyncio
async def test_codex_partial_trailing_line_is_left_unconsumed(tmp_path: Path):
    body = (CODEX_FIXTURES / "basic.jsonl").read_bytes()
    path = _codex_rollout(tmp_path, body=body)
    r = CodexTranscriptReader(base_dir=tmp_path)
    _, off = await r.read_new(path, 0)
    with path.open("ab") as f:
        f.write(b'{"timestamp":"2026-08-21T20:29:00.000Z","type":"event_msg","payl')
    entries, off2 = await r.read_new(path, off)
    assert entries == [] and off2 == off
    with path.open("ab") as f:
        f.write(b'oad":{"type":"agent_message","message":"done"}}\n')
    entries2, off3 = await r.read_new(path, off2)
    assert [e.text for e in entries2] == ["done"]
    assert off3 == path.stat().st_size
