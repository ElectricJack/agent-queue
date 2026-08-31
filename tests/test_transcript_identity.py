"""Historical transcripts must never follow a reused workspace."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from src.sessions.transcripts.claude import ClaudeTranscriptReader
from src.sessions.transcripts.codex import CodexTranscriptReader

OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
AQ = "33333333-3333-4333-8333-333333333333"
START = 1788188504.0


def rollout(root, key, timestamp=START, cwd="/slot", day="2026/08/31"):
    folder = root / ".codex/sessions" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-08-31T08-00-00-{key}.jsonl"
    stamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    path.write_text(json.dumps({"type": "session_meta", "timestamp": stamp,
        "payload": {"id": key, "cwd": cwd, "timestamp": stamp}}) + "\n")
    return path


def row(**kw):
    return SimpleNamespace(id=AQ, session_key=kw.get("key", AQ),
        work_dir="/slot", started_at=kw.get("started_at", START), harness="codex")


def test_missing_known_codex_key_never_falls_back_to_newer_cwd(tmp_path):
    rollout(tmp_path, NEW)
    assert CodexTranscriptReader(tmp_path).resolve_path("/slot", OLD) is None


def test_known_codex_key_is_not_hidden_by_recent_scan_limit(tmp_path, monkeypatch):
    old = rollout(tmp_path, OLD, day="2025/01/01")
    rollout(tmp_path, NEW)
    monkeypatch.setattr("src.sessions.transcripts.codex._MAX_SCAN", 1)
    assert CodexTranscriptReader(tmp_path).resolve_path("/slot", OLD) == old


def test_legacy_aq_uuid_uses_launch_time_not_newest_workdir(tmp_path):
    old = rollout(tmp_path, OLD)
    rollout(tmp_path, NEW, START + 3600)
    assert CodexTranscriptReader(tmp_path).resolve_session(row()) == old


def test_keyless_session_rejects_previous_workspaces_conversation(tmp_path):
    rollout(tmp_path, OLD, START - 300)
    assert CodexTranscriptReader(tmp_path).resolve_session(row(key=None)) is None


def test_discovery_refuses_ambiguous_launches(tmp_path):
    rollout(tmp_path, OLD, START)
    rollout(tmp_path, NEW, START + 1)
    assert CodexTranscriptReader(tmp_path).resolve_session(row(key=None)) is None


def test_known_key_remains_authoritative_even_when_another_launch_is_nearby(tmp_path):
    old = rollout(tmp_path, OLD)
    rollout(tmp_path, NEW, START + 1)
    assert CodexTranscriptReader(tmp_path).resolve_session(row(key=OLD)) == old


def test_missing_claude_key_never_uses_other_conversation(tmp_path):
    directory = tmp_path / ".claude/projects/-slot"
    directory.mkdir(parents=True)
    (directory / (NEW + ".jsonl")).write_text("")
    assert ClaudeTranscriptReader(tmp_path).resolve_path("/slot", OLD) is None
