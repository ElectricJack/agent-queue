"""Tests for aq-surface Phase S2 scope checker."""

from __future__ import annotations

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.scope import AGENT_COMMAND_SET, check_command_scope


SESSION = RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p1")


class TestCheckCommandScope:
    def test_local_scope_allows_anything(self):
        assert check_command_scope("literally_anything", {"x": 1}, LOCAL_SCOPE) is None
        assert check_command_scope("delete_project", {}, LOCAL_SCOPE) is None

    def test_session_scope_allows_agent_command(self):
        assert check_command_scope("task_show", {"task_id": "t1"}, SESSION) is None

    def test_session_scope_blocks_non_agent_command(self):
        msg = check_command_scope("delete_project", {}, SESSION)
        assert msg is not None
        assert "out of scope" in msg
        assert "delete_project" in msg

    def test_task_id_mismatch_blocked(self):
        msg = check_command_scope("task_show", {"task_id": "other"}, SESSION)
        assert msg is not None and "task_id mismatch" in msg

    def test_project_id_mismatch_blocked(self):
        msg = check_command_scope("message_send", {"project_id": "other"}, SESSION)
        assert msg is not None and "project_id mismatch" in msg

    def test_session_id_mismatch_blocked(self):
        msg = check_command_scope("task_heartbeat", {"session_id": "sX"}, SESSION)
        assert msg is not None and "session_id mismatch" in msg

    def test_missing_task_id_is_allowed(self):
        # prime resolves task_id server-side from the scope — omission is OK.
        assert check_command_scope("prime", {}, SESSION) is None

    def test_matching_ids_are_allowed(self):
        assert (
            check_command_scope(
                "task_close",
                {"task_id": "t1", "project_id": "p1", "session_id": "s1"},
                SESSION,
            )
            is None
        )

    def test_agent_command_set_contents(self):
        expected = {
            "prime",
            "get_schema",
            "task_show",
            "task_set",
            "task_close",
            "task_heartbeat",
            "task_handoff",
            "ask_human",
            "message_send",
            "message_inbox",
            "message_reply",
            "memory_save",
            "memory_search",
        }
        assert set(AGENT_COMMAND_SET) == expected
