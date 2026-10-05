"""Command-surface scope invariant (test-coverage plan, commands 25).

Two layers, both derived from the real sources rather than a hand list:

1. **Whole-surface, definition-level.** Every command in
   :data:`src.api.scope.AGENT_COMMAND_SET` is put through
   :func:`src.api.scope.check_command_scope` with a session scope.  The scope
   layer never consults a schema, so this needs no per-command business
   inputs and covers the entire agent surface: omitted-ID injection, foreign-ID
   rejection, and outright refusal of commands outside the set.  The set is
   cross-checked against ``src.mcp_registration.effective_tool_definitions`` (the
   real command-schema registry — ``CommandHandler`` has no per-command schema
   API) so neither the allowlist nor the no-definition exemption list can grow
   silently.

2. **Representative dispatch matrix.** A small, fully-specified set of real
   ``CommandHandler.execute()`` calls: spoofed ``_scope`` is popped, a matching
   scope succeeds, and a foreign ID is refused — for the mutating ``task_set``,
   with no state change.  ``tests/test_task_command_authorization.py`` carries
   the deeper end-to-end mutating pair.

3. **Injected-ID/schema alignment.** Layer 1b proves the gate *injects* the ID
   triple.  A command whose handler then validates that same dict against an
   ``extra="forbid"`` model has to declare all three, or the injection is a
   validation error the agent cannot fix by changing its arguments.  The set of
   such commands is derived from the source, so the guard survives new
   registrations.

4. **The commands the gate cannot pin.** A per-project elevated token is
   enforced two ways — a contract that declares ``project_id``, or a resolvable
   target for :mod:`src.api.target_scope` — and a command with neither gets
   neither.  Layer 1d derives that set from the shipped supervisor grants, the
   contract registry and the argument schemas, and dispatches each
   session-addressing member against another project's session.
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any

import pytest
from pydantic import ValidationError

from src.api.auth import RequestScope
from src.api.scope import _TASK_ID_UNPINNED, AGENT_COMMAND_SET, check_command_scope
from src.mcp_registration import effective_tool_definitions
from src.models import Project, SessionRecord, Task

_ID_KEYS = ("task_id", "project_id", "session_id")

#: In-set commands with no entry in the tool-definition registry.  Asserted as
#: an exact equality so the exemption cannot quietly grow.
_NO_DEFINITION = {"prime", "session_drain_ack"}

#: ``message_inbox`` addresses a mailbox by ``to_kind``/``to_id``, so it has
#: no ID-triple property in its schema — the plan's floor listed it, but the
#: registry disagrees.  It is still fenced by the scope layer, which is
#: schema-independent; the whole-surface tests below prove that for it like
#: every other in-set command.  Asserted explicitly so the distinction is not
#: mistaken for drift.
_NOT_SCHEMA_ID_BEARING = {"message_inbox"}

#: Commands that must stay ID-bearing — a drift guard on the derivation.
_ID_BEARING_FLOOR = {
    "task_show",
    "task_set",
    "task_close",
    "task_heartbeat",
    "task_handoff",
    "message_send",
    "task_claim",
    "create_task",
    "create_task_graph",
}

#: Registered commands deliberately *outside* the agent surface.
_NOT_IN_SET = ["delete_task", "update_config"]


def _definitions_by_name() -> dict[str, dict]:
    # Legacy ``_ALL_TOOL_DEFINITIONS`` plus contract-backed definitions
    # (``integration_status`` carries its schema on a command contract).
    return {d["name"]: d for d in effective_tool_definitions()}


def _session_scope() -> RequestScope:
    return RequestScope(
        kind="session", session_id="s1", task_id="t1", project_id="p1", elevated=False
    )


# ---------------------------------------------------------------------------
# Layer 1a — the derivation itself
# ---------------------------------------------------------------------------


def test_agent_command_set_definition_coverage_and_id_bearing_derivation():
    defs = _definitions_by_name()

    # Every in-set command has a tool definition except the known exemptions.
    missing = {name for name in AGENT_COMMAND_SET if name not in defs}
    assert missing == _NO_DEFINITION

    id_bearing = {
        name
        for name in AGENT_COMMAND_SET
        if name in defs
        and set(defs[name].get("input_schema", {}).get("properties", {})) & set(_ID_KEYS)
    }

    assert id_bearing, "derivation produced no ID-bearing commands — the schema shape changed"
    assert _ID_BEARING_FLOOR <= id_bearing, _ID_BEARING_FLOOR - id_bearing
    assert not (_NOT_SCHEMA_ID_BEARING & id_bearing)
    for name in _NOT_SCHEMA_ID_BEARING:
        assert name in AGENT_COMMAND_SET, name

    # The commands used as the not-in-set sample really are registered
    # commands that the agent surface excludes.
    for name in _NOT_IN_SET:
        assert name in defs, name
        assert name not in AGENT_COMMAND_SET, name


# ---------------------------------------------------------------------------
# Layer 1b — whole-surface scope contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", sorted(AGENT_COMMAND_SET - _TASK_ID_UNPINNED))
def test_omitted_ids_are_injected_from_the_session_scope(command):
    """(b) An omitted ID is filled from the token, never a daemon-side default."""
    scope = _session_scope()
    args: dict = {}

    assert check_command_scope(command, args, scope) is None

    assert args == {"task_id": "t1", "project_id": "p1", "session_id": "s1"}


def test_the_task_id_pin_is_lifted_for_exactly_the_commands_that_name_another_task():
    """``reparent_task`` moves and ``task_route`` re-routes a worker filing,
    never the held task (§12; mandatory routing §7).

    The exemption is server-owned and deliberately tiny: any name entering it
    must be a command whose ``task_id`` is by construction *not* the held task
    and that authorises that task itself -- against the held task, or against
    the session that filed it.
    """
    assert _TASK_ID_UNPINNED == {"reparent_task", "task_route"}
    assert _TASK_ID_UNPINNED <= AGENT_COMMAND_SET


@pytest.mark.parametrize("command", sorted(_TASK_ID_UNPINNED))
def test_unpinned_commands_keep_the_named_task_and_still_inject_the_rest(command):
    scope = _session_scope()
    args: dict = {"task_id": "t2"}

    assert check_command_scope(command, args, scope) is None

    assert args == {"task_id": "t2", "project_id": "p1", "session_id": "s1"}


@pytest.mark.parametrize("command", sorted(AGENT_COMMAND_SET))
@pytest.mark.parametrize(
    ("key", "foreign"), [("task_id", "t2"), ("project_id", "p2"), ("session_id", "s2")]
)
def test_foreign_ids_are_rejected_for_every_agent_command(command, key, foreign):
    """(d) A mismatching ID is refused whichever of the triple it is.

    The one carve-out is ``task_id`` on the commands in ``_TASK_ID_UNPINNED``,
    whose ``task_id`` names a task other than the held one by design; their
    ``project_id`` / ``session_id`` are pinned like everyone else's.
    """
    if key == "task_id" and command in _TASK_ID_UNPINNED:
        pytest.skip("task_id names the moved task, not the held one; pinned in-handler")
    scope = _session_scope()
    args = {key: foreign}

    error = check_command_scope(command, args, scope)

    assert error == f"out of scope: {key} mismatch"


@pytest.mark.parametrize("command", sorted(AGENT_COMMAND_SET))
def test_matching_ids_are_accepted_for_every_agent_command(command):
    """Positive control for the rejection above."""
    scope = _session_scope()
    args = {"task_id": "t1", "project_id": "p1", "session_id": "s1"}

    assert check_command_scope(command, args, scope) is None
    assert args == {"task_id": "t1", "project_id": "p1", "session_id": "s1"}


@pytest.mark.parametrize("command", _NOT_IN_SET)
def test_commands_outside_the_agent_set_are_refused_outright(command):
    scope = _session_scope()
    args: dict = {}

    assert check_command_scope(command, args, scope) == f"out of scope: {command}"
    # A refused command is never given injected IDs.
    assert args == {}


# ---------------------------------------------------------------------------
# Layer 1c — the injected IDs must be declarable by the command's own contract
# ---------------------------------------------------------------------------


def _dispatch_validated_args_models() -> dict[str, Any]:
    """Agent-surface commands that validate the *raw* args dict themselves.

    ``CommandHandler.execute`` does not validate args against a contract — most
    handlers read what they need — so a command opts into strict validation by
    calling its own ``args_model.model_validate(args)``.  Those are exactly the
    commands the gate's in-place injection can break, so they are derived here
    from the registered contract plus the handler source rather than listed.
    """
    from src.commands.contracts import CONTRACTS
    from src.commands.handler import CommandHandler

    found: dict[str, Any] = {}
    for name in sorted(AGENT_COMMAND_SET):
        registration = CONTRACTS.get(name)
        handler = getattr(CommandHandler, f"_cmd_{name}", None)
        if registration is None or handler is None:
            continue
        model = registration.contract.execution.args_model
        if f"{model.__name__}.model_validate(args)" in inspect.getsource(handler):
            found[name] = model
    return found


def test_the_dispatch_validating_derivation_is_not_silently_empty():
    """Guard the guard: if the derivation ever stops finding handlers the layer
    below would pass vacuously."""
    derived = _dispatch_validated_args_models()

    assert "task_handoff" in derived
    assert len(derived) > 1


@pytest.mark.parametrize("command", sorted(_dispatch_validated_args_models()))
def test_every_dispatch_validating_command_accepts_the_injected_id_triple(command):
    """The bug: ``aq handoff --auto`` from a worker died with

        Invalid handoff: TaskHandoffArgs project_id
        Extra inputs are not permitted (input_value agent-queue)

    because :func:`check_command_scope` injects the token's ``project_id`` into
    the very dict the handler validates.  The CLI has no ``--project-id`` and
    ``aq prime`` documents none, so the agent could not do anything about it.

    Declaring the field is a contract alignment, not a scope widening: layer 1b
    still rejects a foreign ``project_id`` before dispatch, and
    ``_assert_session_owns`` still fences task/session/claim_epoch.

    Only the injected IDs are asserted.  A command may of course still refuse
    the triple for a missing business field it requires — that is the caller's
    job, not the gate's — so the check is that no injected ID is rejected as an
    extra.  ``extra`` is asserted forbidden first so a model loosened to
    ``extra="allow"`` cannot pass this by tolerating everything.
    """
    model = _dispatch_validated_args_models()[command]
    injected = {"task_id": "t1", "project_id": "p1", "session_id": "s1"}

    assert model.model_config.get("extra") == "forbid", command
    try:
        model.model_validate(injected)
    except ValidationError as exc:
        forbidden = [error["loc"] for error in exc.errors() if error["type"] == "extra_forbidden"]
        assert not forbidden, f"{command} refuses scope-injected {forbidden}"


async def test_worker_handoff_survives_the_ids_the_scope_gate_injects(matrix):
    """End-to-end through the boundary that failed: the gate mutates ``args``,
    ``execute()`` forwards that same dict, ``_cmd_task_handoff`` validates it.

    Every structured field ``aq prime`` documents is accepted, and the note the
    agent asked for is what gets stored.
    """
    scope = _session_scope()
    args: dict = {
        "auto": True,
        # A pool session must read its epoch from .aq/claim.json; the CLI does.
        "claim_epoch": (await matrix.db.get_task("t1")).claim_epoch,
        "goal": "Ship the handoff fix",
        "completed": ["Reproduced the extra-inputs rejection"],
        "next_step": "Push the branch",
        "constraints": ["Do not weaken the scope gate"],
        "evidence": ["aq test tests/test_command_scope_matrix.py"],
    }

    assert check_command_scope("task_handoff", args, scope) is None
    assert args["project_id"] == "p1"  # the gate injected what the CLI never sent

    args["_scope"] = _scope(task_id="t1")
    result = await matrix.execute("task_handoff", args)

    assert result.get("success"), result
    rows = await matrix.db.get_task_contexts("t1")
    note = json.loads(next(row["content"] for row in rows if row["id"] == result["handoff_id"]))
    assert note["agent"]["goal"] == "Ship the handoff fix"
    assert note["agent"]["next_step"] == "Push the branch"
    assert note["agent"]["constraints"] == ["Do not weaken the scope gate"]
    # The injected scope identity is not agent prose and stays out of the note.
    assert "project_id" not in note["agent"]
    assert not result["restart_requested"]  # --auto is note-only


async def test_a_foreign_project_id_is_still_refused_before_the_handoff_is_validated(matrix):
    """The alignment must not become a hole: the gate still compares the ID it
    injected against the one the caller supplied, and refuses the foreign one."""
    args: dict = {"auto": True, "goal": "g", "project_id": "p2"}

    assert check_command_scope("task_handoff", args, _session_scope()) == (
        "out of scope: project_id mismatch"
    )
    # Refused before dispatch, so nothing is written.
    assert await matrix.db.get_task_contexts("t1") == []


#: The only commands a projectless (manually opened) worker terminal may run:
#: ``prime`` / ``get_schema`` carry no project data, and ``subagent_event``
#: writes one telemetry row keyed by the caller's own ``session_id`` (see the
#: comment in ``check_command_scope``).
_PROJECTLESS_ALLOWED = {"prime", "get_schema", "subagent_event"}


def test_a_worker_terminal_without_a_project_may_only_prime_read_schema_and_report_subagents():
    scope = RequestScope(kind="session", session_id="s1", task_id=None, project_id=None)

    for command in sorted(_PROJECTLESS_ALLOWED):
        assert check_command_scope(command, {}, scope) is None
    for command in sorted(AGENT_COMMAND_SET - _PROJECTLESS_ALLOWED):
        assert (
            check_command_scope(command, {}, scope)
            == "out of scope: this interactive agent has no assigned project"
        )


def test_local_scope_bypasses_the_gate_entirely():
    args = {"task_id": "t2", "project_id": "p2"}
    assert check_command_scope("delete_task", args, RequestScope(kind="local")) is None
    assert args == {"task_id": "t2", "project_id": "p2"}


# ---------------------------------------------------------------------------
# Layer 1d — the commands the per-project gate cannot pin
# ---------------------------------------------------------------------------

#: ``check_command_scope`` enforces ``project_id`` for a per-project elevated
#: token in one of two ways: a command whose contract declares the field is
#: pinned by injection, and a contractless one is checked by resolving the
#: target it names (:mod:`src.api.target_scope`).  An *unregistered* command
#: gets neither: :func:`src.api.scope._forbids_project_id` is False for it, so
#: the gate injects ``project_id`` on the assumption that the handler reads
#: it.  Isolation for those commands is the handler's own job, and the
#: supervisor grant is what makes them reachable at all from a token that is
#: trusted for one project only.
#:
#: Derived from three sources rather than listed, so a new grant, a new
#: contract or a new schema moves the set with it: the shipped supervisor
#: profile's ``aq_commands``, the contract registry, and the argument schema
#: every transport resolves (``_ALL_TOOL_DEFINITIONS`` plus the fallback
#: schemas ``session_*`` lives in).
_SESSION_TARGET = "session_id"

#: In the derived set, but names no row the caller chooses: ``_cmd_prime``
#: reads the *authenticated scope's* own ``session_id`` (``surface_commands``)
#: and never ``args``, so there is no cross-project target to refuse.  Stated
#: here rather than filtered silently, and the dispatch below covers the rest.
_SCOPE_DERIVED_IDENTITY = {"prime"}


def _supervisor_granted_commands() -> frozenset[str]:
    """The commands the shipped supervisor profile grants."""
    from pathlib import Path

    from src.profiles.drift import shipped_profile_path
    from src.profiles.parser import parse_profile

    text = Path(shipped_profile_path("supervisor")).read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.capabilities is not None
    return frozenset(parsed.capabilities["aq_commands"])


def _argument_properties() -> dict[str, set[str]]:
    """Command -> the argument names its schema declares, every transport's view."""
    from src.mcp_registration import _discover_all_commands, effective_tool_definitions

    properties: dict[str, set[str]] = {}
    for definition in (*effective_tool_definitions(), *_discover_all_commands().values()):
        properties.setdefault(definition["name"], set()).update(
            (definition.get("input_schema") or {}).get("properties", {})
        )
    return properties


def _unpinnable_target_commands() -> dict[str, frozenset[str]]:
    """The gap class: command -> the project-owned targets its schema names.

    Every member is granted to the supervisor, has no contract registration,
    and names at least one target reference — so a per-project supervisor token
    can point it at another project's row and nothing in the scope layer reads
    the ``project_id`` it injects.  Some of those targets
    :mod:`src.api.target_scope` could resolve (``task_id``, ``session_id``,
    ``batch_id``), which is exactly what makes their absence a gap rather than
    a limitation; the rest are fenced in-handler by other means.
    """
    from src.api.target_scope import TARGET_REFERENCE_NAMES
    from src.commands.contracts import CONTRACTS

    properties = _argument_properties()
    found: dict[str, frozenset[str]] = {}
    for command in sorted(_supervisor_granted_commands()):
        if CONTRACTS.get(command) is not None:
            continue
        names = properties.get(command, set())
        targets = frozenset(
            name
            for name in names
            # ``project_id`` is what the gate injects, never a caller-named row.
            if name != "project_id"
            and (name.endswith("_id") or name in TARGET_REFERENCE_NAMES)
        )
        if targets:
            found[command] = targets
    return found


def _unpinnable_session_commands() -> frozenset[str]:
    """The subset of :func:`_unpinnable_target_commands` that names a session.

    Each member takes a ``session_id`` — which ``_resolve_session`` also
    answers for a unique id prefix, a name or a task — while ``session_id``
    *is* a registered target resolver, so nothing pins it and each handler has
    to fence the resolved session against the caller's own project.
    """
    return frozenset(
        command
        for command, targets in _unpinnable_target_commands().items()
        if _SESSION_TARGET in targets and command not in _SCOPE_DERIVED_IDENTITY
    )


def _dispatchable_session_commands() -> list[str]:
    """The derived session subset, as a non-empty list of real commands."""
    derived = _unpinnable_session_commands()
    assert derived, "the unpinnable-session derivation found nothing; it cannot be trusted"
    return sorted(derived)


def test_the_unpinnable_session_set_is_derived_and_holds_the_filed_commands():
    """Guard the guard, and name the finding in one place.

    ``session_kill`` and ``provider_reroute_undo`` were each reachable from a
    supervisor of project A and acted on project B's rows.  The sibling task
    ``quick-crest-28`` owns ``provider_reroute_undo``'s handler fence; this
    file pins that both commands are in the derived gap class, and dispatches
    only the session family, which is what this task owns.
    """
    from src.api.target_scope import TARGET_RESOLVERS

    gap = _unpinnable_target_commands()

    assert {"session_kill", "provider_reroute_undo"} <= set(gap)
    # Each names a target the scope layer could have resolved, so registering a
    # contract (declaring no ``project_id``) would hand it to
    # ``target_scope_error`` instead of leaving it to the handler.
    for command in ("session_kill", "provider_reroute_undo"):
        assert gap[command] & set(TARGET_RESOLVERS), command
    # The whole session family the derivation reaches, so a new unfenced
    # session command cannot appear without this test noticing.
    assert _dispatchable_session_commands() == [
        "session_drain_ack", "session_kill", "session_logs", "session_peek",
    ]


@pytest.fixture
async def matrix_foreign_session(command_handler_factory):
    """A live project-B session for the project-A supervisor token below."""
    handler = await command_handler_factory()
    db = handler.db
    await db.create_project(Project(id="p1", name="One", repo_url=""))
    await db.create_project(Project(id="p2", name="Two", repo_url=""))
    await db.create_task(Task(id="t1", project_id="p1", title="own", description=""))
    await db.create_task(Task(id="t2", project_id="p2", title="foreign", description=""))
    await db.create_session(
        SessionRecord(
            id="s2",
            project_id="p2",
            profile_id="generic",
            harness="claude",
            provider="anthropic",
            name="n-s2",
            lifecycle="pool",
            work_dir="/tmp/ws",
            epoch="e1",
            instance_token="tok-s2",
            started_at=time.time(),
            task_id="t2",
            state="running",
            desired_state="running",
        )
    )
    return handler, "s2"


@pytest.mark.parametrize("command", _dispatchable_session_commands())
async def test_a_project_supervisor_cannot_reach_another_projects_session(
    command, matrix_foreign_session
):
    """(d″) The per-project elevation a supervisor token carries, end to end.

    ``session_kill`` stops a worker, ``session_peek`` reads its output,
    ``session_drain_ack`` asks for its teardown and ``session_logs`` reads its
    transcript; all four resolve their argument the same way, so each is
    dispatched here the way the file under test resolves it, against a session
    belonging to the *other* project.  The refusal has to arrive before
    anything is read, written or signalled — hence the unchanged row.

    Handlers are called directly with ``_current_scope`` set rather than
    through ``execute``: its capability gate resolves the principal from the
    session row, and a scope naming a session that does not exist fails closed
    to ``DENY_ALL`` before the fence is reached.  The fence under test reads
    nothing else.
    """
    handler, session_id = matrix_foreign_session

    handler._current_scope = {
        "kind": "session",
        "session_id": "supervisor-p1",
        "task_id": None,
        "project_id": "p1",
        "elevated": True,
    }
    try:
        result = await getattr(handler, f"_cmd_{command}")({"session_id": session_id})
    finally:
        handler._current_scope = None

    assert result.get("success") is not True, result
    assert "out of scope" in result.get("error", ""), result
    row = await handler.db.get_session(session_id)
    assert (row.state, row.desired_state) == ("running", "running")


# ---------------------------------------------------------------------------
# Layer 2 — representative dispatch matrix through execute()
# ---------------------------------------------------------------------------


@pytest.fixture
async def matrix(command_handler_factory):
    """Two projects, one task each, and a pool session bound to t1/p1."""
    handler = await command_handler_factory()
    handler.config.messages.enabled = True
    db = handler.db

    await db.create_project(Project(id="p1", name="One", repo_url=""))
    await db.create_project(Project(id="p2", name="Two", repo_url=""))
    await db.create_task(Task(id="t1", project_id="p1", title="own", description=""))
    await db.create_task(Task(id="t2", project_id="p2", title="foreign", description=""))
    await db.create_session(
        SessionRecord(
            id="s1",
            project_id="p1",
            profile_id="generic",
            harness="claude",
            provider="anthropic",
            name="n-s1",
            lifecycle="pool",
            work_dir="/tmp/ws",
            epoch="e1",
            instance_token="tok-s1",
            started_at=time.time(),
            task_id="t1",
            state="running",
        )
    )
    return handler


def _scope(**overrides) -> dict:
    scope = {
        "kind": "session",
        "session_id": "s1",
        "task_id": None,  # pool token: the claim moves, so no task is pinned
        "project_id": "p1",
        "elevated": False,
    }
    scope.update(overrides)
    return scope


_READS = ["task_show", "task_children", "task_progress"]


@pytest.mark.parametrize("command", _READS)
async def test_matching_scope_reads_succeed_and_foreign_reads_are_refused(matrix, command):
    """(c) positive control + (d′) foreign-ID rejection through ``execute()``."""
    own = await matrix.execute(command, {"task_id": "t1", "_scope": _scope()})
    assert "error" not in own, own

    foreign = await matrix.execute(command, {"task_id": "t2", "_scope": _scope()})
    assert foreign["success"] is False
    assert foreign["result"] == "out_of_scope"
    assert "outside this session's scope" in foreign["error"]


@pytest.mark.parametrize("command", _READS + ["message_inbox"])
async def test_spoofed_scope_in_client_args_is_popped_before_dispatch(matrix, command):
    """(a) A client-supplied ``_scope`` never reaches the handler as an argument.

    ``execute()`` pops ``_scope`` off ``args`` and exposes it only via
    ``self._current_scope``; the trusted envelope is injected by the API layer.
    A call carrying a spoofed elevated/global envelope must therefore produce
    the same result as the same call with no envelope at all — the spoof buys
    nothing and never lands in the handler's args.
    """
    base = (
        {"to_kind": "session", "to_id": "s1"} if command == "message_inbox" else {"task_id": "t1"}
    )
    spoofed = dict(base)
    spoofed["_scope"] = {
        "kind": "session",
        "session_id": "s1",
        "task_id": None,
        "project_id": None,
        "elevated": True,
    }

    seen: list[dict] = []
    real = getattr(matrix, f"_cmd_{command}")

    async def _spy(args: dict) -> dict:
        seen.append(dict(args))
        return await real(args)

    setattr(matrix, f"_cmd_{command}", _spy)

    with_spoof = await matrix.execute(command, spoofed)
    without = await matrix.execute(command, dict(base))

    assert with_spoof == without
    for args in seen:
        assert "_scope" not in args


async def test_task_set_accepts_the_held_task_and_refuses_a_foreign_one(matrix):
    """(c) + (d′) for the mutating command, with a persisted no-change check."""
    task = await matrix.db.get_task("t1")

    ok = await matrix.execute(
        "task_set",
        {
            "task_id": "t1",
            "branch": "feature/x",
            "claim_epoch": task.claim_epoch,
            "_scope": _scope(),
        },
    )
    assert ok.get("success") is not False, ok
    assert (await matrix.db.get_task("t1")).branch_name == "feature/x"

    before = await matrix.db.get_task("t2")
    refused = await matrix.execute(
        "task_set",
        {
            "task_id": "t2",
            "branch": "feature/hijack",
            "claim_epoch": before.claim_epoch,
            "_scope": _scope(),
        },
    )
    assert refused["success"] is False
    assert refused["result"] == "out_of_scope"
    assert "does not hold task t2" in refused["error"]

    # The rejection changed nothing.
    after = await matrix.db.get_task("t2")
    assert after.branch_name == before.branch_name
    assert after.status == before.status
    assert after.updated_at == before.updated_at


async def test_message_inbox_refuses_a_system_mailbox_for_a_plain_session(matrix):
    result = await matrix.execute(
        "message_inbox",
        {"to_kind": "session", "to_id": "supervisor-global", "_scope": _scope()},
    )

    assert "error" in result
    assert "global" in result["error"].lower() or "scope" in result["error"].lower()
