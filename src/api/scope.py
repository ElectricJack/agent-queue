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
        "task_heartbeat",
        "task_handoff",
        "ask_human",
        "message_send",
        "message_inbox",
        "message_reply",
        "memory_save",
        "memory_search",
    }
)


def check_command_scope(command: str, args: dict, scope: RequestScope) -> str | None:
    if scope.kind == "local":
        return None
    if command not in AGENT_COMMAND_SET:
        return f"out of scope: {command}"
    for key, expected in (
        ("task_id", scope.task_id),
        ("project_id", scope.project_id),
        ("session_id", scope.session_id),
    ):
        value = args.get(key)
        if value is not None and expected is not None and value != expected:
            return f"out of scope: {key} mismatch"
    return None
