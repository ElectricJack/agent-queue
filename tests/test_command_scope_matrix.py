"""Command-surface scope invariant (test-coverage plan, commands 25).

Two layers, both derived from the real sources rather than a hand list:

1. **Whole-surface, definition-level.** Every command in
   :data:`src.api.scope.AGENT_COMMAND_SET` is put through
   :func:`src.api.scope.check_command_scope` with a session scope.  The scope
   layer never consults a schema, so this needs no per-command business
   inputs and covers the entire agent surface: omitted-ID injection, foreign-ID
   rejection, and outright refusal of commands outside the set.  The set is
   cross-checked against ``src.tools.definitions._ALL_TOOL_DEFINITIONS`` (the
   real command-schema registry — ``CommandHandler`` has no per-command schema
   API) so neither the allowlist nor the no-definition exemption list can grow
   silently.

2. **Representative dispatch matrix.** A small, fully-specified set of real
   ``CommandHandler.execute()`` calls: spoofed ``_scope`` is popped, a matching
   scope succeeds, and a foreign ID is refused — for the mutating ``task_set``,
   with no state change.  ``tests/test_task_command_authorization.py`` carries
   the deeper end-to-end mutating pair.
"""

from __future__ import annotations

import time

import pytest

from src.api.auth import RequestScope
from src.api.scope import AGENT_COMMAND_SET, check_command_scope
from src.models import Project, SessionRecord, Task
from src.tools.definitions import _ALL_TOOL_DEFINITIONS

_ID_KEYS = ("task_id", "project_id", "session_id")

#: In-set commands with no entry in the tool-definition registry.  Asserted as
#: an exact equality so the exemption cannot quietly grow.
#: ``subagent_event`` is the harness-hook receiver behind ``aq subagent event``
#: (``src/api/scope.py``): a CLI/hook-only command like ``prime``, so it is
#: left to auto-discovery rather than given an LLM-facing schema.
_NO_DEFINITION = {"prime", "session_drain_ack", "subagent_event"}

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
}

#: Registered commands deliberately *outside* the agent surface.
_NOT_IN_SET = ["delete_task", "create_task_graph", "update_config"]


def _definitions_by_name() -> dict[str, dict]:
    return {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}


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


@pytest.mark.parametrize("command", sorted(AGENT_COMMAND_SET))
def test_omitted_ids_are_injected_from_the_session_scope(command):
    """(b) An omitted ID is filled from the token, never a daemon-side default."""
    scope = _session_scope()
    args: dict = {}

    assert check_command_scope(command, args, scope) is None

    assert args == {"task_id": "t1", "project_id": "p1", "session_id": "s1"}


@pytest.mark.parametrize("command", sorted(AGENT_COMMAND_SET))
@pytest.mark.parametrize(
    ("key", "foreign"), [("task_id", "t2"), ("project_id", "p2"), ("session_id", "s2")]
)
def test_foreign_ids_are_rejected_for_every_agent_command(command, key, foreign):
    """(d) A mismatching ID is refused whichever of the triple it is."""
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
