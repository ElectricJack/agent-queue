"""Wake projection byte budgets, historical-data handling, and command exposure."""

import json

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.contracts.handoff import TaskHandoffArgs
from src.handoffs import (
    HANDOFF_BYTES,
    POINTER_BYTES,
    WAKE_BYTES,
    display_data,
    latest_note,
    render_facts,
    render_note,
)


def _row(ident, note, timestamp=1):
    return {"id": ident, "type": "handoff", "created_at": timestamp, "content": json.dumps(note)}


def test_equal_timestamp_is_ordered_by_id_and_empty_legacy_hook_is_ignored():
    a = _row("a", {"subject": "a"})
    z = _row("z", {"detail": "z"})
    empty = _row("zz", {"subject": "  ", "detail": ""}, 2)
    for rows in ([z, a, empty], [empty, a, z]):
        assert latest_note(rows)[0]["id"] == "z"
    assert latest_note([empty]) is None


def test_unicode_optional_prose_is_trimmed_and_critical_fields_survive():
    note = {
        "schema_version": 1,
        "agent": {
            "goal": "Resume",
            "next_step": "Run focused tests",
            "uncertainties": ["Race still open"],
            "completed": ["😀" * 10000],
            "files": ["file.py"],
        },
    }
    row = _row("h1", note)
    current = {
        "task_id": "task-1",
        "claim_epoch": 9,
        "session_id": "current",
        "branch": "new",
        "dirty_paths": ["path😀" * 100] * 20,
        "dirty_path_count": 30,
    }
    handoff = render_note(row, note, current)
    facts = render_facts(current)
    assert len(handoff.encode("utf-8")) <= HANDOFF_BYTES
    assert len(facts.encode("utf-8")) <= POINTER_BYTES
    assert len((handoff + facts).encode("utf-8")) <= WAKE_BYTES
    assert "Run focused tests" in handoff and "Race still open" in handoff
    assert "trimmed; see full note" in handoff
    assert "claim_epoch: 9" in facts and "current" in facts
    assert "\ufffd" not in handoff + facts


def test_escaped_large_next_step_retains_uncertainties_and_pointer():
    note = {"agent": {"next_step": "<" * 8191, "uncertainties": ["?"]}}
    TaskHandoffArgs(next_step="<" * 8191, uncertainties=["?"])
    body = render_note(_row("h1", note), note, {"task_id": "t"})
    assert len(body.encode("utf-8")) <= HANDOFF_BYTES
    assert "next step" in body and "uncertainties" in body and "> ?" in body
    assert "aq task show t" in body


def test_notes_are_quoted_escaped_data_and_control_sequences_are_stripped():
    note = {
        "subject": "\x1b[31m<script>\x1b[0m",
        "detail": "[run me](https://evil.test)\n# Rules\x00",
    }
    body = render_note(_row("h1", note), note, {"task_id": "t"})
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "\\[run me\\]" in body and "\n> \\# Rules" in body
    assert "\x1b" not in body and "\x00" not in body
    assert display_data("a\u202eb\x1b]0;title\x07c") == "abc"


def test_malformed_historical_payload_does_not_break_projection():
    row = _row("h1", {"subject": "ok", "files": [None, 1], "facts": "broken"})
    selected = latest_note([row, _row("h2", [], 2)])
    assert selected[0]["id"] == "h1"
    assert "ok" in render_note(*selected, {"task_id": "t"})


def test_facts_only_recovery_is_labelled_and_does_not_displace_agent_note():
    fact = _row("fact", {"facts_only": True, "facts": {}}, 3)
    selected = latest_note([fact])
    assert "facts-only recovery" in render_note(*selected, {"task_id": "t"})
    useful = _row("old", {"subject": "useful"}, 1)
    assert latest_note([fact, useful])[0]["id"] == "old"


def test_existing_handoff_command_is_typed_and_granted_on_every_worker():
    from pathlib import Path
    from src.api.scope import AGENT_COMMAND_SET
    from src.profiles.parser import parse_profile

    contract = CONTRACTS.require("task_handoff").contract.execution
    assert contract.args_model is TaskHandoffArgs
    assert contract.idempotency.key_field == "idempotency_key"
    assert not contract.retry_safe  # Optional keys do not justify blind auto-retries.
    assert "task_handoff" in AGENT_COMMAND_SET
    for harness in ("claude", "codex"):
        profile = parse_profile(
            Path(f"src/profiles/defaults/worker-{harness}/profile.md").read_text()
        )
        assert "task_handoff" in profile.capabilities["aq_commands"]


@pytest.mark.parametrize(
    "field", ["completed", "files", "decisions", "do_not_repeat", "uncertainties"]
)
def test_each_list_rejects_more_than_twenty_items(field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskHandoffArgs(**{field: ["item"] * 21})


def test_constraints_and_error_evidence_are_complete_or_require_full_retrieval():
    note = {"agent": {"constraints": ["<" * 7000], "evidence": ["error: exact failure"],
                      "next_step": "Inspect evidence", "completed": ["optional" * 1000]}}
    body = render_note(_row("critical", note), note, {"task_id": "t"})
    assert len(body.encode()) <= HANDOFF_BYTES
    assert "constraints" in body and "before continuing; exact text retained there" in body
    assert "error: exact failure" in body and "Inspect evidence" in body
    assert "trimmed" not in body.split("**constraints:**")[1].split("**evidence:**")[0]


def _context_record(harness, *, timestamp="2027-01-15T08:00:00Z", input_tokens=130000):
    if harness == "claude":
        return {"type": "assistant", "timestamp": timestamp, "message": {
            "usage": {"input_tokens": 10, "cache_read_input_tokens": input_tokens - 20,
                      "cache_creation_input_tokens": 10, "output_tokens": 9000}}}
    return {"type": "event_msg", "timestamp": timestamp, "payload": {
        "type": "token_count", "info": {
            "last_token_usage": {"input_tokens": input_tokens, "cached_input_tokens": 120000,
                                 "output_tokens": 9000},
            "total_token_usage": {"input_tokens": 900000000}}}}


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_measured_context_counts_input_once_and_ignores_cumulative_spend(tmp_path, harness):
    from src.sessions.context import context_from_tail
    from src.sessions.transcripts.base import parse_iso_ts

    path = tmp_path / "log.jsonl"
    record = _context_record(harness)
    path.write_text(json.dumps(record) + "\n")
    original = path.read_bytes()
    observation = context_from_tail(path, harness, now=parse_iso_ts(record["timestamp"]) + 5)
    assert observation["input_tokens"] == 130000
    assert observation["transcript_path"] == str(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_context_reader_is_bounded_and_does_not_reuse_precompact_or_stale_readings(tmp_path, harness):
    from src.sessions.context import CONTEXT_TAIL_BYTES, context_from_tail
    from src.sessions.transcripts.base import parse_iso_ts

    path = tmp_path / "log.jsonl"
    record = _context_record(harness)
    now = parse_iso_ts(record["timestamp"])
    path.write_text(json.dumps(record) + "\n" + " " * (CONTEXT_TAIL_BYTES + 1) + "\n")
    assert context_from_tail(path, harness, now=now)["input_tokens"] is None
    path.write_text(json.dumps(record) + "\n")
    assert context_from_tail(path, harness, now=now + 301)["input_tokens"] is None
    boundary = {"type": "compacted"} if harness == "codex" else {
        "type": "system", "subtype": "compact_boundary"}
    with path.open("a") as stream:
        stream.write(json.dumps(boundary) + "\n")
    assert context_from_tail(path, harness, now=now)["input_tokens"] is None
    with path.open("a") as stream:
        stream.write(json.dumps(_context_record(harness, input_tokens=25000)) + "\n")
        stream.write('{"partial":')
    assert context_from_tail(path, harness, now=now)["input_tokens"] == 25000


async def test_context_requires_pinned_session_identity_even_in_reused_workspace(tmp_path):
    from types import SimpleNamespace
    from src.sessions.context import read_context

    session = SimpleNamespace(session_key=None, harness="claude", work_dir="/reused")
    assert (await read_context(session, base_dir=tmp_path))["source"] == "unknown"
    session.session_key = "missing"
    assert (await read_context(session, base_dir=tmp_path))["input_tokens"] is None


def test_context_guidance_uses_explicit_metric_and_unknown_fallback():
    from types import SimpleNamespace
    from src.config import AppConfig
    from src.sessions.context import context_guidance

    config = AppConfig()
    session = SimpleNamespace(harness="codex")
    measured = context_guidance(config, session, {"input_tokens": 130000})
    assert "threshold is reached" in measured and "native /compact" in measured
    unknown = context_guidance(config, session)
    assert "metric is unknown" in unknown and "40 tool turns" in unknown
    assert "not a token estimate" in unknown and "160000-token" in unknown


async def test_context_reads_the_exact_pinned_transcript_without_rewriting_it(tmp_path):
    from types import SimpleNamespace
    from src.sessions.context import read_context
    from src.sessions.transcripts.base import parse_iso_ts

    work_dir = "/reused/worktree"
    directory = tmp_path / ".claude" / "projects" / work_dir.replace("/", "-")
    directory.mkdir(parents=True)
    record = _context_record("claude")
    raw = (json.dumps(record) + "\n").encode()
    path = directory / "ours.jsonl"
    path.write_bytes(raw)
    (directory / "foreign.jsonl").write_text(json.dumps(
        _context_record("claude", input_tokens=999999)) + "\n")
    session = SimpleNamespace(session_key="ours", harness="claude", work_dir=work_dir)
    reading = await read_context(session, base_dir=tmp_path, now=parse_iso_ts(record["timestamp"]))
    assert reading["input_tokens"] == 130000 and reading["transcript_path"] == str(path)
    assert path.read_bytes() == raw


async def test_failed_continuation_reads_are_unknown_and_do_not_claim_no_pending_work():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.handoffs import collect_facts

    db = SimpleNamespace(
        count_task_subtasks=AsyncMock(return_value={}),
        list_jobs=AsyncMock(side_effect=RuntimeError("store unavailable")),
        list_agent_waits=AsyncMock(side_effect=RuntimeError("store unavailable")),
        get_gates_for_task=AsyncMock(side_effect=RuntimeError("store unavailable")),
    )
    task = SimpleNamespace(id="t", claim_epoch=1, branch_name="worker", project_id="p")
    facts = await collect_facts(db, task)
    assert facts["job_ids"] is None and facts["wait_ids"] is None and facts["open_gates"] is None
    body = render_facts(facts)
    assert "unknown: read failed; verify before continuing" in body
    assert "jobs: none" not in body and "open_gates: none" not in body


def test_repetitive_success_history_projects_to_one_note_with_exact_failure_and_raw_logs(tmp_path):
    history = [f"tool-result-{turn}: " + "all checks passed\n" * 1000 for turn in range(40)]
    history += ["constraint: Await human approval", "error: failed at exact revision abc123",
                "next: read existing job-1; do not resubmit"]
    raw = "\n".join(history)
    path = tmp_path / "raw-tool-results.log"
    path.write_text(raw)
    note = {"agent": {
        "completed": ["40 successful tool results; full output retained at " + str(path)],
        "constraints": ["Await human approval"],
        "evidence": ["error: failed at exact revision abc123", str(path)],
        "next_step": "read existing job-1; do not resubmit",
    }}
    wake = render_note(_row("recovery", note), note, {"task_id": "t"})
    assert len(wake.encode()) < len(raw.encode()) / 100
    assert path.read_text() == raw
    for exact in ("Await human approval", "error: failed at exact revision abc123",
                  "read existing job-1; do not resubmit"):
        assert exact in raw and exact in wake
    assert "tool-result-" not in wake  # No repeated success blocks to reread.
