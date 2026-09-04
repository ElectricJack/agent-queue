"""Tests for aq-surface Phase S2 scope checker."""

from __future__ import annotations

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.scope import AGENT_COMMAND_SET, check_command_scope


SESSION = RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p1")


#: The exact server-owned agent allowlist (``src/api/scope.py``).
#: Pinned so a name can only enter it deliberately — Playbook V2
#: Package 0 §3.7 explicitly does not widen this set to make room for
#: the capability gate.
EXPECTED_AGENT_COMMANDS = {
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
    "subagent_event",
    "reparent_task",
}


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

    def test_per_project_elevation_injects_matching_project_and_preserves_task_session_fences(
        self,
    ):
        """Plan 8: elevation relaxes the command set and ONLY project identity.

        A per-project supervisor token may run any command, but always inside
        its own project: an omitted project_id is injected, a matching one
        passes untouched, a foreign one is rejected.  Elevation must not
        widen a plain agent token's task/session fences either — those
        mismatches keep rejecting for non-elevated scopes.
        """
        elevated = RequestScope(
            kind="session", session_id="sup-1", project_id="p1", elevated=True,
        )

        # Omitted → injected (any command, not just AGENT_COMMAND_SET).
        args: dict = {}
        assert check_command_scope("delete_project", args, elevated) is None
        assert args == {"project_id": "p1"}

        # Matching → allowed and preserved.
        args = {"project_id": "p1", "name": "renamed"}
        assert check_command_scope("edit_project", args, elevated) is None
        assert args["project_id"] == "p1"

        # Foreign → rejected, args untouched beyond the read.
        args = {"project_id": "p2"}
        msg = check_command_scope("edit_project", args, elevated)
        assert msg is not None and "project_id mismatch" in msg
        assert args == {"project_id": "p2"}

        # Elevation never leaks task/session identity into the args: only
        # project scope is injected, unlike the plain-session path.
        args = {}
        elevated_with_task = RequestScope(
            kind="session", session_id="sup-1", task_id="t-sup",
            project_id="p1", elevated=True,
        )
        assert check_command_scope("task_show", args, elevated_with_task) is None
        assert args == {"project_id": "p1"}

        # A plain (non-elevated) token's task/session fences still reject.
        plain = RequestScope(kind="session", session_id="s1", task_id="t1", project_id="p1")
        msg = check_command_scope("task_show", {"task_id": "other"}, plain)
        assert msg is not None and "task_id mismatch" in msg
        msg = check_command_scope("task_heartbeat", {"session_id": "sX"}, plain)
        assert msg is not None and "session_id mismatch" in msg

    def test_agent_command_set_contents(self):
        assert set(AGENT_COMMAND_SET) == EXPECTED_AGENT_COMMANDS

    def test_reparent_task_names_a_task_other_than_the_held_one(self):
        """The moved task is a worker filing, never the held task itself.

        The generic ``task_id`` pin would refuse every worker reparent on a
        task-lifecycle token; authorisation for the *moved* task is derived
        from the held task inside ``_cmd_reparent_task`` instead.
        """
        args = {"task_id": "t1.2", "parent_id": "epic"}
        assert check_command_scope("reparent_task", args, SESSION) is None
        assert args["task_id"] == "t1.2"
        assert args["project_id"] == "p1" and args["session_id"] == "s1"

    def test_reparent_task_still_pins_project_and_session(self):
        other = {"task_id": "t1.2", "root": True, "project_id": "p2"}
        msg = check_command_scope("reparent_task", other, SESSION)
        assert msg is not None and "project_id mismatch" in msg


class TestScopeAndCapabilityCompose:
    """Playbook V2 Package 0 §3.7 — two independent gates, not one.

    ``check_request_scope`` is a *server-owned* allowlist: the same names
    for every non-elevated session, regardless of profile. The capability
    gate is *profile-owned*. A command must pass both. Package 0 deliberately
    does not relax the scope gate to make room for the new one, and does not
    widen ``AGENT_COMMAND_SET`` to shrink the §1.5 unreachable set.
    """

    def test_the_scope_gate_is_unchanged_by_package_0(self):
        """The scope allowlist is server-owned, and Package 0 adds nothing to it.

        Pinned against the shared ``EXPECTED_AGENT_COMMANDS`` literal
        rather than a bare count, so a name added for reasons of its own
        updates exactly one place, while a name *this* package tried to
        slip in breaks both tests.
        """
        assert set(AGENT_COMMAND_SET) == EXPECTED_AGENT_COMMANDS
        assert check_command_scope("task_show", {"task_id": "t1"}, SESSION) is None
        assert check_command_scope("delete_task", {}, SESSION) is not None

    def test_a_profile_policy_cannot_widen_the_scope_gate(self):
        """Naming a command in a profile does not make the token admit it.

        The capability gate can only *narrow*: a command outside
        AGENT_COMMAND_SET is refused at the scope boundary before dispatch
        is reached, whatever the profile says.
        """
        from src.profiles.capabilities import CapabilityPolicy

        policy = CapabilityPolicy.from_namespaces(aq_commands=["delete_task"])
        assert policy.allows_aq_command("delete_task") is True
        assert check_command_scope("delete_task", {}, SESSION) is not None

    def test_capability_denial_is_a_second_independent_gate(self):
        """A command the scope gate admits can still be denied by policy."""
        from src.commands.authorization import authorize_command
        from src.commands.principal import ExecutionPrincipal, PrincipalKind
        from src.profiles.capabilities import CapabilityPolicy

        class _Resolver:
            def is_builtin(self, name):
                return True

            def is_plugin(self, name):
                return False

            def plugin_command_names(self):
                return frozenset()

        assert "task_claim" in AGENT_COMMAND_SET
        assert check_command_scope("task_claim", {}, SESSION) is None

        principal = ExecutionPrincipal(
            kind=PrincipalKind.SESSION,
            policy=CapabilityPolicy.from_namespaces(aq_commands=["task_show"]),
        )
        decision = authorize_command(
            "task_claim", principal, resolver=_Resolver(), mode="enforce"
        )
        assert decision.allowed is False
