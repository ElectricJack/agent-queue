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

    def test_scope_ids_injected_when_client_omits(self):
        """I3: omitted task_id/project_id/session_id are filled from the scope.

        The command must not fall back to daemon-side defaults (e.g. the
        ``_active_project_id`` ContextVar) when the token itself defines
        the identity.  Mirrors what ``_cmd_prime`` / ``_cmd_task_handoff``
        already do explicitly for their own reads of ``_current_scope``.
        """
        args: dict = {}
        assert check_command_scope("task_show", args, SESSION) is None
        assert args["task_id"] == "t1"
        assert args["project_id"] == "p1"
        assert args["session_id"] == "s1"

    def test_scope_injection_preserves_explicit_value(self):
        args = {"task_id": "t1"}  # matches — must not be overwritten
        assert check_command_scope("task_show", args, SESSION) is None
        assert args["task_id"] == "t1"
        assert args["project_id"] == "p1"  # injected
        assert args["session_id"] == "s1"  # injected

    def test_scope_injection_does_not_apply_to_local(self):
        args: dict = {}
        assert check_command_scope("task_show", args, LOCAL_SCOPE) is None
        assert args == {}  # LOCAL_SCOPE never mutates

    def test_scope_injection_via_stub_command(self):
        """Observed at the command boundary: a session-scoped call missing
        ``project_id`` sees the token's ``project_id`` in its args dict."""
        # /api/execute forwards the (mutated) args to CommandHandler.execute,
        # so a stub command would receive the injected fields.  We assert on
        # ``args`` directly here since check_command_scope is where the
        # mutation happens; the execute path is exercised by test_api_auth's
        # middleware tests.
        args: dict = {"foo": "bar"}
        assert check_command_scope("memory_search", args, SESSION) is None
        assert args["project_id"] == "p1"
        assert args["task_id"] == "t1"
        assert args["foo"] == "bar"

    def test_agent_can_drain_ack_its_own_session(self):
        """The completion protocol's second half must be reachable.

        ``aq task close`` answers ``next_step: run `aq session drain-ack```
        and ``task_claim`` answers ``session_exhausted``/``drain_requested``
        with the same instruction — an agent that cannot run it strands its
        session (and its workspace lock) until a reconciler backstop fires.
        """
        args: dict = {}
        assert check_command_scope("session_drain_ack", args, SESSION) is None
        assert args["session_id"] == "s1"

    def test_agent_cannot_drain_ack_another_session(self):
        msg = check_command_scope("session_drain_ack", {"session_id": "sX"}, SESSION)
        assert msg is not None and "session_id mismatch" in msg

    def test_agent_command_set_contents(self):
        expected = {
            "prime",
            "get_schema",
            "task_show",
            "task_set",
            "task_comment",
            "task_comments",
            "task_close",
            "task_children",
            "task_progress",
            "task_heartbeat",
            "task_handoff",
            "ask_human",
            "message_send",
            "message_inbox",
            "message_reply",
            "memory_save",
            "memory_search",
            "task_claim",
            "session_drain_ack",
            "create_task",
            "project_ready",
            "formula_list",
            "formula_show",
        }
        assert set(AGENT_COMMAND_SET) == expected
