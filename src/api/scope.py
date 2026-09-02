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
        "task_comment",
        "task_comments",
        "task_close",
        "task_children",
        "task_progress",
        "task_heartbeat",
        "task_handoff",
        # The two subagent lifecycle hooks report through here.  A session
        # may only write its own telemetry: ``_cmd_subagent_event`` binds
        # the row to ``scope.session_id`` and ignores any session named in
        # the payload.
        "subagent_event",
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
    if command == "edit_intelligence_class" and not (
        scope.elevated and scope.project_id is None and scope.task_id is None
    ):
        return "out of scope: intelligence-class settings require global admin"
    if command == "session_input" and not (scope.elevated and scope.project_id is None):
        return "out of scope: direct terminal input requires global admin"
    if command in {"create_agent", "edit_agent", "delete_agent", "start_agent_terminal"} and not (
        scope.elevated and scope.project_id is None
    ):
        return "out of scope: global agent settings require global admin"
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
    # A manually opened worker terminal has no task/project assignment.
    # Its absent project must not grant access to every project's mutations.
    # Assigned task/pool sessions always carry a concrete project scope.
    # ``subagent_event`` joins prime/get_schema here because it carries no
    # project data at all: it writes one row keyed by the caller's own
    # ``session_id``.  A manually opened terminal that could not report its
    # sub-agents would be silently mis-counted rather than protected.
    if scope.project_id is None and command not in {
        "prime", "get_schema", "subagent_event",
    }:
        return "out of scope: this interactive agent has no assigned project"
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


# A triage task needs to inspect and route its project's queue. These are
# capabilities of a saved, actively assigned triage session, never elevation
# inferred from client arguments or the worker's model/name.
_TRIAGE_COMMANDS = frozenset({
    "list_tasks", "get_task", "task_show", "gate_list", "gate_show",
    "list_profiles", "list_intelligence_classes", "task_route",
})

_PLAYBOOK_COMPILER_COMMANDS = frozenset({"playbook_validate", "playbook_install"})


async def _has_live_playbook_compiler_assignment(db, scope: RequestScope) -> bool:
    """Grant compiler mutations only to the exact active compiler claim."""
    from src.models import AgentState, TaskStatus

    if db is None or not scope.session_id or not scope.task_id or not scope.project_id:
        return False
    session = await db.get_session(scope.session_id)
    if (
        session is None
        or session.task_id != scope.task_id
        or session.project_id != scope.project_id
        or session.profile_id != "playbook-compiler"
        or session.lifecycle != "task"
        or session.state not in {"starting", "running"}
        or session.desired_state != "running"
        or not session.agent_id
    ):
        return False
    task = await db.get_task(scope.task_id)
    if (
        task is None
        or task.project_id != scope.project_id
        or task.profile_id != "playbook-compiler"
        or task.status != TaskStatus.IN_PROGRESS
        or task.assigned_agent_id != session.agent_id
        or task.claim_epoch != session.last_claim_epoch
    ):
        return False
    agent = await db.get_agent(session.agent_id)
    return bool(
        agent is not None
        and agent.enabled
        and agent.deleted_at is None
        and agent.state == AgentState.BUSY
        and agent.current_task_id == task.id
    )


async def _has_live_triage_assignment(db, scope: RequestScope) -> bool:
    from src.models import AgentState, TaskStatus

    if db is None or not scope.session_id or not scope.project_id:
        return False
    session = await db.get_session(scope.session_id)
    triage_profiles = {"triage", f"project:{scope.project_id}:triage"}
    if (
        session is None
        or session.project_id != scope.project_id
        or session.profile_id not in triage_profiles
        or session.lifecycle not in {"task", "pool"}
        or session.state not in {"starting", "running"}
        or session.desired_state != "running"
        or not session.task_id
        or not session.agent_id
        or (scope.task_id is not None and scope.task_id != session.task_id)
        or (session.lifecycle == "task" and scope.task_id != session.task_id)
    ):
        return False
    task = await db.get_task(session.task_id)
    if (
        task is None
        or task.project_id != scope.project_id
        or task.profile_id not in triage_profiles
        or task.status != TaskStatus.IN_PROGRESS
        or task.assigned_agent_id != session.agent_id
    ):
        return False
    agent = await db.get_agent(session.agent_id)
    return bool(
        agent is not None
        and agent.enabled
        and agent.deleted_at is None
        and agent.state == AgentState.BUSY
        and agent.current_task_id == task.id
    )


async def check_request_scope(
    command: str, args: dict, scope: RequestScope, *, db=None,
) -> str | None:
    """Apply the normal scope, with narrowly verified triage capabilities.

    Both HTTP command surfaces use this guard. Tokens retain their ordinary
    task/session identity; granting queue access never grants operator commands
    or loosens the ownership checks for task mutations such as task_close.
    """
    if (
        scope.kind == "session"
        and not scope.elevated
        and command in _PLAYBOOK_COMPILER_COMMANDS
    ):
        if not await _has_live_playbook_compiler_assignment(db, scope):
            return check_command_scope(command, args, scope)
        for key, expected in (
            ("task_id", scope.task_id),
            ("project_id", scope.project_id),
            ("session_id", scope.session_id),
        ):
            value = args.get(key)
            if value is None:
                args[key] = expected
            elif value != expected:
                return f"out of scope: {key} mismatch"
        return None

    if scope.kind != "session" or scope.elevated or command not in _TRIAGE_COMMANDS:
        return check_command_scope(command, args, scope)

    ordinary_args = dict(args)
    error = check_command_scope(command, ordinary_args, scope)
    if error is None:
        args.update(ordinary_args)
        return None
    if not await _has_live_triage_assignment(db, scope):
        return error

    project_id = scope.project_id
    if args.get("project_id") not in (None, project_id):
        return "out of scope: project_id mismatch"
    if args.get("session_id") not in (None, scope.session_id):
        return "out of scope: session_id mismatch"

    if command in {"get_task", "task_show", "task_route"}:
        task_id = args.get("task_id")
        task = await db.get_task(str(task_id)) if task_id else None
        if task is None or task.project_id != project_id:
            return "out of scope: task must belong to this triage project's queue"
        if command == "task_route":
            profile_id = str(args.get("profile_id") or "")
            if profile_id.startswith("project:") and not profile_id.startswith(
                f"project:{project_id}:"
            ):
                return "out of scope: profile belongs to another project"
            gates = await db.get_gates_for_task(task.id)
            if not any(
                gate["project_id"] == project_id
                and gate["gate_type"] == "routing"
                and gate["status"] == "open"
                for gate in gates
            ):
                return "out of scope: triage may only route tasks with an open routing gate"
    elif command == "gate_show":
        gate_id = args.get("gate_id")
        gate = await db.get_gate(str(gate_id)) if gate_id else None
        if (
            gate is None
            or gate["project_id"] != project_id
            or gate["gate_type"] != "routing"
            or gate["status"] != "open"
        ):
            return "out of scope: triage may only read its project's open routing gates"
    elif command == "gate_list":
        if args.get("gate_type") not in (None, "routing") or args.get("status") not in (
            None, "open",
        ):
            return "out of scope: triage may only read open routing gates"
        args["gate_type"] = "routing"
        args["status"] = "open"

    args["project_id"] = project_id
    args["session_id"] = scope.session_id
    return None
