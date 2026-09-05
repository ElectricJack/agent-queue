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
        # A worker may move a task it filed (swarm-work-model §12): the
        # command re-derives the held task from the session and admits only
        # worker-filed tasks provenance-linked to it, moved to a parent the
        # filing path itself would have accepted.
        "reparent_task",
    }
)

#: Commands whose ``task_id`` names a task *other than* the held one, so the
#: held-task pin below must not apply to it.  ``reparent_task`` moves a
#: worker filing, which by construction is never the task the token holds;
#: ``_cmd_reparent_task`` authorises the moved task against the held task.
#: ``project_id`` and ``session_id`` stay pinned.
_TASK_ID_UNPINNED: frozenset[str] = frozenset({"reparent_task"})

#: The seven project-onboarding commands (design 2026-09-03 §5), gated to
#: the loopback CLI and the global admin (§7).  Listed literally because this
#: module is a leaf (``src.profiles.capabilities`` imports it) and cannot
#: reach ``src.commands.contracts.project_onboarding.ONBOARDING_COMMANDS``
#: without a cycle; ``tests/test_project_onboarding_contract.py`` pins the
#: two sets equal.
PROJECT_ONBOARDING_COMMANDS: frozenset[str] = frozenset(
    {
        "list_project_roots",
        "browse_project_root",
        "get_github_auth_status",
        "list_github_owners",
        "search_github_repositories",
        "onboard_project",
        "get_project_onboarding",
    }
)
PROJECT_ONBOARDING_SCOPE_ERROR = "out of scope: project onboarding requires global admin"


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
    # Project onboarding (design 2026-09-03 §7): browsing configured roots,
    # GitHub discovery and the ``onboard_project`` saga are filesystem
    # authorisation on the daemon host.  They create a project rather than
    # act within one, so a per-project supervisor is refused too — only the
    # loopback CLI (returned above) and the global admin may run them.
    if command in PROJECT_ONBOARDING_COMMANDS and not (
        scope.elevated and scope.project_id is None
    ):
        return PROJECT_ONBOARDING_SCOPE_ERROR
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
        if key == "task_id" and command in _TASK_ID_UNPINNED:
            continue
        value = args.get(key)
        if value is None:
            if expected is not None:
                args[key] = expected
            continue
        if expected is not None and value != expected:
            return f"out of scope: {key} mismatch"
    return None


# Every worker profile's close protocol ends with "push the branch and open a
# PR", so a task-scoped token has to be able to do exactly that -- and nothing
# more.  These are capabilities of a live worker session that holds an
# IN_PROGRESS task in this project; the grant reaches only that task's own
# branch.  ``git_merge`` and ``pr_merge`` stay out: merging is the
# final-reviewer's authority (see ``_FINAL_REVIEWER_COMMANDS``).
_WORKER_GIT_READ_COMMANDS = frozenset({
    "get_git_status", "git_diff", "git_log", "git_branch", "git_changed_files",
})
_WORKER_GIT_WRITE_COMMANDS = frozenset({"git_push", "git_create_pr"})
_WORKER_GIT_COMMANDS = _WORKER_GIT_READ_COMMANDS | _WORKER_GIT_WRITE_COMMANDS


async def worker_branches_for_session(db, scope: RequestScope) -> frozenset[str] | None:
    """Return the branch names a live worker session may act on, else ``None``.

    Authority comes from persisted state only: the session row, the task it
    holds, and the agent that holds it.  Both lifecycles are covered --
    ``task`` (pushed work) and ``pool`` (a claimed task) -- because both close
    through the same protocol.  The branch set is the task's recorded
    ``branch_name`` plus the conventional ``aq/<task_id>``; a client-supplied
    branch outside it is refused.
    """
    from src.models import AgentState, TaskStatus

    if db is None or not scope.session_id or not scope.project_id:
        return None
    session = await db.get_session(scope.session_id)
    if (
        session is None
        or session.project_id != scope.project_id
        or session.lifecycle not in {"task", "pool"}
        or session.state not in {"starting", "running"}
        or session.desired_state != "running"
        or not session.task_id
        or not session.agent_id
        or (scope.task_id is not None and scope.task_id != session.task_id)
        or (session.lifecycle == "task" and scope.task_id != session.task_id)
    ):
        return None
    task = await db.get_task(session.task_id)
    if (
        task is None
        or task.project_id != scope.project_id
        or task.status != TaskStatus.IN_PROGRESS
        or task.assigned_agent_id != session.agent_id
        or (
            session.last_claim_epoch is not None
            and session.last_claim_epoch != task.claim_epoch
        )
    ):
        return None
    agent = await db.get_agent(session.agent_id)
    if not (
        agent is not None
        and agent.enabled
        and agent.deleted_at is None
        and agent.state == AgentState.BUSY
        and agent.current_task_id == task.id
    ):
        return None
    branches = {f"aq/{task.id}"}
    if task.branch_name:
        branches.add(task.branch_name)
    return frozenset(branches)


async def _check_worker_git_scope(
    command: str, args: dict, scope: RequestScope, *, db,
) -> str | None:
    """Gate the worker git carve-out to the session's own task branch."""
    branches = await worker_branches_for_session(db, scope)
    if branches is None:
        return f"out of scope: {command}"
    if args.get("project_id") not in (None, scope.project_id):
        return "out of scope: project_id mismatch"
    if args.get("session_id") not in (None, scope.session_id):
        return "out of scope: session_id mismatch"
    project = await db.get_project(scope.project_id)
    default_branch = (getattr(project, "repo_default_branch", None) or "main") if project else "main"
    # ``branch``/``name`` name the head a command would act on; ``None`` means
    # "the branch this worktree is already on", which is the task's own.
    # ``git_branch`` with a name creates and checks out -- only the task's own
    # branch qualifies; without one it merely lists.
    if (
        command == "git_branch"
        and args.get("name") is not None
        and args["name"] not in branches
    ):
        return "out of scope: branch mismatch"
    if command in {"git_push", "git_create_pr"}:
        branch = args.get("branch")
        if branch is not None and branch not in branches:
            return "out of scope: branch mismatch"
    if command == "git_create_pr":
        base = args.get("base")
        if base is not None and base != default_branch:
            return "out of scope: branch mismatch"
    args["project_id"] = scope.project_id
    args["session_id"] = scope.session_id
    return None


# A triage task needs to inspect and route its project's queue. These are
# capabilities of a saved, actively assigned triage session, never elevation
# inferred from client arguments or the worker's model/name.
_TRIAGE_COMMANDS = frozenset({
    "list_tasks", "get_task", "task_show", "gate_list", "gate_show",
    "list_profiles", "list_intelligence_classes", "task_route",
})

_PLAYBOOK_COMPILER_COMMANDS = frozenset(
    {
        "playbook_validate",
        "playbook_install",
        "playbook_v2_validate",
        "playbook_v2_propose",
    }
)

# A reviewer task's whole job is a verdict on *another* task: read it, and
# either approve (close its own review task) or reject.  Rejection is
# ``reopen_with_feedback`` on the reviewed task, which is neither in
# AGENT_COMMAND_SET nor addressable under the token's own ``task_id`` pin.
# These are capabilities of a saved, actively assigned reviewer session, and
# they reach exactly one task: the one this review was spawned for.
_REVIEWER_COMMANDS = frozenset({
    "reopen_with_feedback", "task_show", "get_task", "task_comments",
})

# A final review is a branch-wide verdict.  Its authority is derived from the
# graph the default pipeline writes: final review ``blocks`` each per-task
# review, and each per-task review has one ``discovered-from`` worker task.
# The worker tasks must agree on one branch and PR URL; ambiguity fails closed.
_FINAL_REVIEWER_COMMANDS = frozenset({
    "reopen_with_feedback", "task_show", "get_task", "task_comments", "pr_merge", "git_diff",
})


async def reviewed_task_for_reviewer(db, scope: RequestScope) -> str | None:
    """Return the one task a live reviewer session may act on, else ``None``.

    Shaped like :func:`_has_live_triage_assignment`, but it resolves a target
    instead of a boolean: the grant is scoped to the reviewed task rather than
    to the project's whole queue.  The reviewed task is taken from the review
    task's ``discovered-from`` edges (written by the ``per-task-review``
    pipeline rule alongside the review task itself).  The human-readable
    description is agent-editable and therefore never participates in the
    authorization decision.
    """
    from src.models import AgentState, TaskStatus

    if db is None or not scope.session_id or not scope.project_id:
        return None
    session = await db.get_session(scope.session_id)
    reviewer_profiles = {"reviewer"}
    if (
        session is None
        or session.project_id != scope.project_id
        or session.profile_id not in reviewer_profiles
        or session.lifecycle not in {"task", "pool"}
        or session.state not in {"starting", "running"}
        or session.desired_state != "running"
        or not session.task_id
        or not session.agent_id
        or (scope.task_id is not None and scope.task_id != session.task_id)
    ):
        return None
    review = await db.get_task(session.task_id)
    if (
        review is None
        or review.project_id != scope.project_id
        or review.profile_id not in reviewer_profiles
        or review.status != TaskStatus.IN_PROGRESS
        or review.assigned_agent_id != session.agent_id
        or (
            session.last_claim_epoch is not None
            and session.last_claim_epoch != review.claim_epoch
        )
    ):
        return None
    agent = await db.get_agent(session.agent_id)
    if not (
        agent is not None
        and agent.enabled
        and agent.deleted_at is None
        and agent.state == AgentState.BUSY
        and agent.current_task_id == review.id
    ):
        return None

    edges = await db.get_typed_dependencies(review.id)
    targets = {dep_id for dep_id, dep_type in edges if dep_type == "discovered-from"}
    if len(targets) != 1:
        return None
    reviewed_id = next(iter(targets))
    reviewed = await db.get_task(reviewed_id)
    if reviewed is None or reviewed.project_id != scope.project_id:
        return None
    return reviewed_id


async def reviewed_branch_for_final_reviewer(
    db, scope: RequestScope,
) -> tuple[frozenset[str], str] | None:
    """Return a live final review's worker tasks and their one PR URL.

    ``final-reviewer`` has exceptional merge authority, so its grant is
    anchored in persisted graph provenance rather than editable task prose or
    client-supplied branch fields.  Every blocked review must resolve to one
    worker on the same branch and PR; a malformed or mixed graph grants
    nothing.
    """
    from src.models import AgentState, TaskStatus

    if db is None or not scope.session_id or not scope.project_id:
        return None
    session = await db.get_session(scope.session_id)
    profiles = {"final-reviewer"}
    if (
        session is None
        or session.task_id != scope.task_id
        or session.project_id != scope.project_id
        or session.profile_id not in profiles
        or session.lifecycle != "task"
        or session.state not in {"starting", "running"}
        or session.desired_state != "running"
        or not session.agent_id
    ):
        return None
    final_review = await db.get_task(session.task_id)
    if (
        final_review is None
        or final_review.project_id != scope.project_id
        or final_review.profile_id not in profiles
        or final_review.status != TaskStatus.IN_PROGRESS
        or final_review.assigned_agent_id != session.agent_id
        or final_review.claim_epoch != session.last_claim_epoch
    ):
        return None
    agent = await db.get_agent(session.agent_id)
    if not (
        agent is not None
        and agent.enabled
        and agent.deleted_at is None
        and agent.state == AgentState.BUSY
        and agent.current_task_id == final_review.id
    ):
        return None

    review_ids = {
        task_id for task_id, dep_type in await db.get_typed_dependencies(final_review.id)
        if dep_type == "blocks"
    }
    if not review_ids:
        return None
    worker_ids: set[str] = set()
    branches: set[str] = set()
    pr_urls: set[str] = set()
    reviewer_profiles = {"reviewer"}
    for review_id in review_ids:
        review = await db.get_task(review_id)
        if (
            review is None
            or review.project_id != scope.project_id
            or review.profile_id not in reviewer_profiles
        ):
            return None
        workers = {
            task_id for task_id, dep_type in await db.get_typed_dependencies(review.id)
            if dep_type == "discovered-from"
        }
        if len(workers) != 1:
            return None
        worker = await db.get_task(next(iter(workers)))
        if (
            worker is None
            or worker.project_id != scope.project_id
            or not worker.branch_name
            or not worker.pr_url
        ):
            return None
        worker_ids.add(worker.id)
        branches.add(worker.branch_name)
        pr_urls.add(worker.pr_url)
    if len(branches) != 1 or len(pr_urls) != 1:
        return None
    return frozenset(worker_ids), next(iter(pr_urls))



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
    triage_profiles = {"triage"}
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

    if scope.kind == "session" and not scope.elevated and command in _WORKER_GIT_COMMANDS:
        ordinary_args = dict(args)
        error = check_command_scope(command, ordinary_args, scope)
        if error is None:
            args.update(ordinary_args)
            return None
        # ``git_diff`` is also a final-reviewer capability; let that carve-out
        # answer for a final-review session rather than pre-empting it.
        worker_error = await _check_worker_git_scope(command, args, scope, db=db)
        if worker_error is None:
            return None
        if command not in _FINAL_REVIEWER_COMMANDS:
            return worker_error

    if scope.kind == "session" and not scope.elevated and command in _REVIEWER_COMMANDS:
        # Ordinary scope first: a reviewer reading or commenting on its *own*
        # review task needs no carve-out, and must not lose the normal
        # injection of task/project/session ids.
        ordinary_args = dict(args)
        error = check_command_scope(command, ordinary_args, scope)
        if error is None:
            args.update(ordinary_args)
            return None
        reviewed_id = await reviewed_task_for_reviewer(db, scope)
        if reviewed_id is not None:
            if args.get("task_id") != reviewed_id:
                return "out of scope: a reviewer may only act on the task it is reviewing"
            if args.get("project_id") not in (None, scope.project_id):
                return "out of scope: project_id mismatch"
            if args.get("session_id") not in (None, scope.session_id):
                return "out of scope: session_id mismatch"
            args["project_id"] = scope.project_id
            args["session_id"] = scope.session_id
            return None
        # Not a reviewer.  ``task_show``/``get_task`` are also triage
        # capabilities, so fall through rather than pre-empting that carve-out.
        if command not in _TRIAGE_COMMANDS and command not in _FINAL_REVIEWER_COMMANDS:
            return error

    if scope.kind == "session" and not scope.elevated and command in _FINAL_REVIEWER_COMMANDS:
        ordinary_args = dict(args)
        error = check_command_scope(command, ordinary_args, scope)
        if error is None:
            args.update(ordinary_args)
            return None
        reviewed_branch = await reviewed_branch_for_final_reviewer(db, scope)
        if reviewed_branch is None:
            # ``task_show``/``get_task`` are also triage capabilities.  A
            # triage session will not resolve a final-review branch, but it
            # must still reach the triage carve-out below.
            if command not in _TRIAGE_COMMANDS:
                return error
        else:
            worker_ids, pr_url = reviewed_branch
            if args.get("project_id") not in (None, scope.project_id):
                return "out of scope: project_id mismatch"
            if args.get("session_id") not in (None, scope.session_id):
                return "out of scope: session_id mismatch"
            branch_bound = {"reopen_with_feedback", "task_show", "get_task", "task_comments"}
            if command == "pr_merge" and args.get("pr_url") != pr_url:
                return "out of scope: a final reviewer may only merge its review branch PR"
            if command in branch_bound and args.get("task_id") not in worker_ids:
                return (
                    "out of scope: a final reviewer may only act on tasks "
                    "from its review branch"
                )
            args["project_id"] = scope.project_id
            args["session_id"] = scope.session_id
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
