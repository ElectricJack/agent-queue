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
