"""Native sub-agent telemetry: hook payloads in, authoritative counts out.

Covers the three seams between a harness and the flock view — parsing the
hook's stdin JSON, recording it exactly once, and scoping it to the session
that reported it.
"""
from __future__ import annotations

import json

import pytest

from src.models import SessionRecord
from src.prime.hook_envelopes import parse_subagent_hook

# Captured live on 2026-09-01 from the two shipped harnesses.  Kept verbatim
# rather than minimised: the point of these fixtures is that the parser is
# tested against what the CLIs actually emit, extra fields and all.
CLAUDE_START = {
    "session_id": "01a05f2b-ef80-7802-aa10-4961d1f7f294",
    "transcript_path": "/home/u/.claude/projects/slug/abc.jsonl",
    "cwd": "/repo",
    "permission_mode": "bypassPermissions",
    "hook_event_name": "SubagentStart",
    "agent_id": "agent_017Kx",
    "agent_type": "Explore",
}
CLAUDE_STOP = {
    "session_id": "01a05f2b-ef80-7802-aa10-4961d1f7f294",
    "transcript_path": "/home/u/.claude/projects/slug/abc.jsonl",
    "cwd": "/repo",
    "hook_event_name": "SubagentStop",
    "stop_hook_active": False,
    "agent_id": "agent_017Kx",
    "agent_transcript_path": "/home/u/.claude/projects/slug/agent_017Kx.jsonl",
    "agent_type": "Explore",
    "last_assistant_message": "done",
}
CODEX_START = {
    "session_id": "01a05f2b-ef80-7802-aa10-4961d1f7f294",
    "turn_id": "01a05f2c-0220-7051-8241-8a253c386fcd",
    "transcript_path": "/home/u/.codex/sessions/2026/09/01/rollout-child.jsonl",
    "cwd": "/repo",
    "hook_event_name": "SubagentStart",
    "model": "gpt-5.6-sol",
    "permission_mode": "bypassPermissions",
    "agent_id": "01a05f2c-01e6-71e0-987c-098befa86df0",
    "agent_type": "default",
}
CODEX_STOP = {
    "session_id": "01a05f2b-ef80-7802-aa10-4961d1f7f294",
    "turn_id": "01a05f2c-0220-7051-8241-8a253c386fcd",
    "transcript_path": "/home/u/.codex/sessions/2026/09/01/rollout-parent.jsonl",
    "agent_transcript_path": "/home/u/.codex/sessions/2026/09/01/rollout-child.jsonl",
    "cwd": "/repo",
    "hook_event_name": "SubagentStop",
    "model": "gpt-5.6-sol",
    "permission_mode": "bypassPermissions",
    "stop_hook_active": False,
    "agent_id": "01a05f2c-01e6-71e0-987c-098befa86df0",
    "agent_type": "default",
    "last_assistant_message": "42",
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_claude_start_and_stop_pair_on_the_same_child_id():
    start = parse_subagent_hook(json.dumps(CLAUDE_START))
    stop = parse_subagent_hook(json.dumps(CLAUDE_STOP))
    assert start["event"] == "start" and stop["event"] == "stop"
    assert start["subagent_id"] == stop["subagent_id"] == "agent_017Kx"
    assert start["agent_type"] == "Explore"
    # Claude sends no turn id; the field is optional, not invented.
    assert start["turn_id"] is None


def test_codex_start_and_stop_pair_and_carry_the_turn():
    start = parse_subagent_hook(json.dumps(CODEX_START))
    stop = parse_subagent_hook(json.dumps(CODEX_STOP))
    assert start["subagent_id"] == stop["subagent_id"]
    assert start["turn_id"] == stop["turn_id"] == "01a05f2c-0220-7051-8241-8a253c386fcd"
    assert stop["event"] == "stop"


def test_the_harness_session_id_is_carried_but_is_not_the_aq_session():
    # Provenance only: the daemon binds the event to the bearer token's own
    # session, so a payload can never name someone else's session.
    parsed = parse_subagent_hook(json.dumps(CODEX_START))
    assert parsed["harness_session_id"] == CODEX_START["session_id"]


@pytest.mark.parametrize("raw", [
    "",
    "not json",
    "[]",
    '"a string"',
    json.dumps({"hook_event_name": "SessionStart", "session_id": "s"}),
    json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"}),
    json.dumps({"agent_id": "x"}),
])
def test_anything_that_is_not_a_subagent_event_parses_to_none_rather_than_raising(raw):
    # This runs inside the agent's own process tree; an exception here would
    # be a hook that stops sub-agents from starting.
    assert parse_subagent_hook(raw) is None


def test_a_missing_agent_id_falls_back_to_the_child_transcript():
    payload = {**CODEX_STOP}
    payload.pop("agent_id")
    parsed = parse_subagent_hook(json.dumps(payload))
    assert parsed["subagent_id"] == CODEX_STOP["agent_transcript_path"]


def test_a_payload_with_no_usable_child_identity_yields_an_empty_id():
    # The CLI drops these rather than collapsing every child onto one row.
    parsed = parse_subagent_hook(json.dumps({"hook_event_name": "SubagentStart"}))
    assert parsed is not None and parsed["subagent_id"] == ""


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _session(session_id="s-1", harness="claude", project_id=None, task_id=None):
    return SessionRecord(
        id=session_id, project_id=project_id, profile_id="implementer",
        harness=harness, provider="fake", name=session_id, lifecycle="task",
        work_dir="/repo", epoch="e", instance_token="tok", started_at=1.0,
        task_id=task_id, state="running", hooks_provisioned=True,
    )


@pytest.fixture
async def handler(command_handler_factory):
    return await command_handler_factory()


async def test_a_start_then_stop_raises_and_lowers_the_active_count(handler):
    await handler.db.create_session(_session())
    first = await handler.execute("subagent_event", {
        "session_id": "s-1", "event": "start", "subagent_id": "a1", "agent_type": "Explore",
    })
    assert first["recorded"] is True
    assert first["active_subagent_count"] == 1
    second = await handler.execute("subagent_event", {
        "session_id": "s-1", "event": "start", "subagent_id": "a2",
    })
    assert second["active_subagent_count"] == 2
    stop = await handler.execute("subagent_event", {
        "session_id": "s-1", "event": "stop", "subagent_id": "a1",
    })
    assert stop["active_subagent_count"] == 1
    assert stop["subagents_spawned_total"] == 2


async def test_a_redelivered_hook_is_recorded_once(handler):
    await handler.db.create_session(_session())
    args = {"session_id": "s-1", "event": "start", "subagent_id": "a1"}
    first = await handler.execute("subagent_event", dict(args))
    again = await handler.execute("subagent_event", dict(args))
    assert first["recorded"] is True
    assert again["recorded"] is False
    # The duplicate is not an error the harness should surface to the agent.
    assert "error" not in again
    assert again["active_subagent_count"] == 1
    assert again["subagents_spawned_total"] == 1


async def test_a_stop_without_a_start_is_tolerated_and_does_not_go_negative(handler):
    await handler.db.create_session(_session())
    result = await handler.execute("subagent_event", {
        "session_id": "s-1", "event": "stop", "subagent_id": "ghost",
    })
    assert result["success"] is True
    assert result["active_subagent_count"] == 0


async def test_events_are_folded_per_session_not_pooled(handler):
    await handler.db.create_session(_session("s-1"))
    await handler.db.create_session(_session("s-2"))
    await handler.execute("subagent_event", {
        "session_id": "s-1", "event": "start", "subagent_id": "a1",
    })
    await handler.execute("subagent_event", {
        "session_id": "s-2", "event": "start", "subagent_id": "a1",
    })
    counts = await handler.db.subagent_counts_by_session()
    assert counts == {
        "s-1": {"starts": 1, "stops": 0},
        "s-2": {"starts": 1, "stops": 0},
    }
    # The same child id in two sessions is two different children.
    assert (await handler.db.subagent_counts_by_session(["s-1"])) == {
        "s-1": {"starts": 1, "stops": 0}
    }


async def test_the_row_records_the_reporting_sessions_harness_and_scope(handler):
    await handler.db.create_session(
        _session("s-1", harness="codex", project_id=None, task_id=None)
    )
    await handler.execute("subagent_event", {
        "session_id": "s-1", "event": "start", "subagent_id": "a1",
        "agent_type": "default", "turn_id": "t-9",
    })
    rows = await handler.db.list_subagent_events("s-1")
    assert len(rows) == 1
    assert rows[0]["harness"] == "codex"
    assert rows[0]["agent_type"] == "default"
    assert rows[0]["turn_id"] == "t-9"
    assert rows[0]["event"] == "start"


@pytest.mark.parametrize("args, expected", [
    ({"session_id": "s-1", "event": "spawn", "subagent_id": "a"}, "event must be"),
    ({"session_id": "s-1", "event": "start"}, "subagent_id is required"),
    ({"session_id": "nope", "event": "start", "subagent_id": "a"}, "No session"),
])
async def test_malformed_or_unknown_input_is_refused_with_a_message(handler, args, expected):
    await handler.db.create_session(_session())
    result = await handler.execute("subagent_event", args)
    assert expected in result["error"]


async def test_the_session_comes_from_the_token_scope_not_the_payload(handler):
    await handler.db.create_session(_session("s-mine"))
    await handler.db.create_session(_session("s-theirs"))
    # ``_scope`` is what /api/execute forwards from the validated bearer
    # token; a client-supplied one is stripped before it gets here.
    result = await handler.execute("subagent_event", {
        "_scope": {"kind": "session", "session_id": "s-mine"},
        "session_id": "s-theirs", "event": "start", "subagent_id": "a1",
    })
    assert result["session_id"] == "s-mine"
    assert (await handler.db.subagent_counts_by_session(["s-theirs"])) == {}


def test_the_hook_receiver_is_in_the_agent_command_set():
    from src.api.scope import AGENT_COMMAND_SET, check_command_scope
    from src.api.auth import RequestScope

    assert "subagent_event" in AGENT_COMMAND_SET
    scope = RequestScope(kind="session", session_id="s-1", project_id="p")
    args: dict = {"event": "start", "subagent_id": "a1"}
    assert check_command_scope("subagent_event", args, scope) is None
    # The session id is injected from the token, so a hook never has to
    # (and never gets to) name one.
    assert args["session_id"] == "s-1"


def test_a_projectless_interactive_terminal_may_still_report_its_children():
    from src.api.scope import check_command_scope
    from src.api.auth import RequestScope

    scope = RequestScope(kind="session", session_id="s-1", project_id=None)
    assert check_command_scope("subagent_event", {}, scope) is None
    # ...but it still cannot reach the rest of the surface.
    assert check_command_scope("task_close", {}, scope) is not None
