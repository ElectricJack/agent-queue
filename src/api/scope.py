"""Pure scope check for session-bound API requests (aq-surface §4.3).

The set is deliberately narrow: agent-surface commands only.  Trusted
callers (CLI on loopback with no bearer) get :data:`LOCAL_SCOPE` and
bypass this check entirely; the middleware is where that dispatch
happens.
"""

from __future__ import annotations

from src.api.auth import RequestScope


AGENT_COMMAND_SET: frozenset[str] = frozenset(
    {
        "prime",
        "get_schema",
        "task_show",
        "task_set",
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
        # The second half of the completion protocol.  ``aq task close``
        # transitions the task; ``aq session drain-ack`` says "I am done,
        # you may kill me" — and it is the documented next move on
        # ``session_exhausted`` and ``drain_requested`` (swarm-work-model
        # §10, and ``_cmd_task_close``'s own ``next_step`` string).  Without
        # it here, the only caller that is ever *supposed* to run it could
        # not: an exhausted pool worker's own token was refused, so the
        # session sat idle holding a workspace until a reconciler backstop
        # noticed.  The ``session_id`` pin below is what keeps a worker from
        # acking anyone else's session.
        "session_drain_ack",
        "create_task",
        "project_ready",
        "formula_list",
        "formula_show",
    }
)


def check_command_scope(command: str, args: dict, scope: RequestScope) -> str | None:
    """Enforce (and, for session scopes, inject) the request's scope.

    For session-scoped requests: when the client omitted ``task_id`` /
    ``project_id`` / ``session_id``, populate them from the token's own
    scope so the command doesn't fall back to daemon-side defaults (e.g.
    ``_active_project_id`` ContextVar) — the token defines the identity.
    Mirrors what ``_cmd_prime`` and ``_cmd_task_handoff`` already do
    explicitly.  Mismatches still reject.

    ``args`` is mutated in place (same object /api/execute forwards to
    ``ch.execute``).
    """
    if scope.kind == "local":
        return None
    # Projectless messages are system records, not an omitted project filter.
    # Null project scope alone must never grant access to the global supervisor.
    system_message = args.get("system_only") or (
        command in {"message_send", "message_inbox"}
        and args.get("to_kind") == "session"
        and args.get("to_id") == "supervisor-global"
    )
    if system_message and not (scope.elevated and scope.project_id is None):
        return "out of scope: system messages require global admin"
    # Elevated session (per-project supervisor OR the global supervisor).
    # Skip the AGENT_COMMAND_SET gate — the supervisor is a trusted
    # operator that runs every ``aq`` command on behalf of the user.
    if scope.elevated:
        # Global admin — elevated + no project scope means the token can
        # touch any command in any project.  Used exclusively by the
        # ``supervisor-global`` session (loopback-restricted at the
        # middleware layer).
        if scope.project_id is None:
            return None
        # Per-project elevated: enforce ``project_id`` so a supervisor
        # for project A cannot mutate B; inject when the caller omits it.
        expected_pid = scope.project_id
        value = args.get("project_id")
        if value is None:
            args["project_id"] = expected_pid
        elif value != expected_pid:
            return "out of scope: project_id mismatch"
        return None
    if command not in AGENT_COMMAND_SET:
        return f"out of scope: {command}"
    for key, expected in (
        ("task_id", scope.task_id),
        ("project_id", scope.project_id),
        ("session_id", scope.session_id),
    ):
        value = args.get(key)
        if value is None:
            if expected is not None:
                args[key] = expected
            continue
        if expected is not None and value != expected:
            return f"out of scope: {key} mismatch"
    return None
