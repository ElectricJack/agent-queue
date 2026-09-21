"""Provider intent: who meant the provider a task's profile names (provider-failover D8-D10).

Every row of the D9 table, the pin permission rule, ``task_route``'s
never-downgrade, the graph ``pin`` key, the playbook ``pin_provider`` step
field, ``explicit_route``'s pinned vendor, ``delete_profile``'s reset and the
``a00000000013`` migration (named constraints, idempotent backfill to
``preferred`` and never ``pinned``, the ``archived_tasks`` mirror).  On
PostgreSQL, like the rest of the suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from src.assignment_routing import ExplicitRouting, explicit_route
from src.commands.contracts.models import (
    CommandArgs,
    CommandContract,
    CommandPresentation,
    CommandResult,
    CommandValue,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.playbooks.definition import canonical_bytes
from src.providers.intent import (
    CLASS_ONLY,
    PINNED,
    PREFERRED,
    effective_intent,
    narrows_catalog,
    resolve_intent,
)
from src.task_graph.formulas import FormulaRegistry, load_from_vault
from src.vault import ensure_default_intelligence_classes
from tests.db_fixtures import lease_dsn

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_CAPS = {"harness_tools": ["Bash", "Edit"], "aq_commands": [], "plugin_tools": []}
WORKER_SCOPE = {"kind": "session", "session_id": "s1", "project_id": "p", "elevated": False}
SUPERVISOR_SCOPE = {"kind": "session", "session_id": "sup", "project_id": "p", "elevated": True}


def _worker(profile_id: str, harness: str, default_class: str, **extra: Any) -> AgentProfile:
    return AgentProfile(
        id=profile_id, name=profile_id, harness=harness, lifecycle="pool",
        default_class=default_class, needs_workspace=False, **{**WORKER_CAPS, **extra},
    )


@pytest.fixture
async def setup(tmp_path):
    db = Database(lease_dsn("provider_intent.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    for profile in (
        _worker("standard-high-claude", "claude", "standard-high"),
        _worker("deep-high-claude", "claude", "deep-high"),
        _worker("standard-high-codex", "codex", "standard-high"),
        _worker("astra-high-codex", "codex", "astra-high"),
    ):
        await db.create_profile(profile)
    await db.update_project("p", default_profile_id="standard-high-claude")
    data_dir = str(tmp_path / "data")
    ensure_default_intelligence_classes(data_dir)
    config = AppConfig(
        data_dir=data_dir, database=DatabaseConfig(url=lease_dsn("provider_intent.db"))
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, config)
    yield handler, db
    handler._current_scope = None
    await db.close()


async def _as(handler, scope, coro_factory):
    handler._current_scope = scope
    try:
        return await coro_factory()
    finally:
        handler._current_scope = None


async def _create(handler, **args):
    return await handler._cmd_create_task({"project_id": "p", "title": "Work", **args})


# -- pure helpers ---------------------------------------------------------------


def test_effective_intent_without_a_profile_is_class_only() -> None:
    assert effective_intent(Task(id="t", project_id="p", title="", description="",
                                 provider_intent=PINNED)) == CLASS_ONLY
    pinned = Task(id="t", project_id="p", title="", description="",
                  profile_id="x", provider_intent=PINNED)
    assert effective_intent(pinned) == PINNED and narrows_catalog(pinned)
    routed = Task(id="t", project_id="p", title="", description="", profile_id="x")
    assert effective_intent(routed) == CLASS_ONLY and not narrows_catalog(routed)


def test_resolve_intent_follows_how_the_profile_was_chosen() -> None:
    assert resolve_intent(None, profile_supplied=True, profile_id="x") == PREFERRED
    assert resolve_intent(None, profile_supplied=False, profile_id="x") == CLASS_ONLY
    assert resolve_intent(None, pin=True, profile_supplied=True, profile_id="x") == PINNED
    assert resolve_intent(CLASS_ONLY, profile_supplied=True, profile_id="x") == CLASS_ONLY
    assert resolve_intent(PINNED, profile_supplied=True, profile_id=None) == CLASS_ONLY


# -- create_task (D9 rows) ----------------------------------------------------


async def test_create_with_explicit_profile_is_preferred(setup) -> None:
    handler, db = setup
    result = await _create(handler, profile_id="standard-high-codex",
                           intelligence_class="standard-high")
    assert result.get("success") is True, result
    assert result["provider_intent"] == PREFERRED
    assert (await db.get_task(result["task_id"])).provider_intent == PREFERRED


async def test_create_with_pin_is_pinned_and_audited(setup) -> None:
    handler, db = setup
    result = await _create(handler, profile_id="standard-high-codex",
                           intelligence_class="standard-high", pin=True)
    assert result.get("success") is True, result
    task = await db.get_task(result["task_id"])
    assert task.provider_intent == PINNED
    audit = await db.get_task_meta(task.id, "provider_intent_audit")
    assert audit["intent"] == PINNED and audit["by"] == "human:local-operator"
    assert audit["previous"] is None and audit["at"] > 0


async def test_create_with_explicit_intent_value(setup) -> None:
    handler, db = setup
    result = await _create(handler, profile_id="standard-high-codex",
                           intelligence_class="standard-high", provider_intent=CLASS_ONLY)
    assert (await db.get_task(result["task_id"])).provider_intent == CLASS_ONLY


async def test_create_by_class_match_is_class_only(setup) -> None:
    handler, db = setup
    result = await _create(handler, intelligence_class="deep-high")
    assert result["profile_source"] == "class_match", result
    assert (await db.get_task(result["task_id"])).provider_intent == CLASS_ONLY


async def test_create_with_nothing_leaves_the_default_implicit_and_class_only(setup) -> None:
    handler, db = setup
    result = await _create(handler)
    task = await db.get_task(result["task_id"])
    assert task.profile_id is None and task.provider_intent == CLASS_ONLY
    assert await db.get_task_meta(task.id, "provider_intent_audit") is None


async def test_pin_without_a_profile_is_refused(setup) -> None:
    handler, db = setup
    result = await _create(handler, pin=True)
    assert result["code"] == "provider_intent.profile_required"
    result = await _create(handler, provider_intent=PREFERRED)
    assert result["code"] == "provider_intent.profile_required"
    assert await db.list_tasks(project_id="p") == []


async def test_unknown_intent_is_refused(setup) -> None:
    handler, _db = setup
    result = await _create(handler, profile_id="standard-high-codex", provider_intent="sticky")
    assert result["code"] == "provider_intent.invalid"


async def test_ensure_task_carries_intent(setup) -> None:
    handler, db = setup
    result = await handler._cmd_ensure_task({
        "project_id": "p", "title": "Ensured", "dedup_key": "k1",
        "profile_id": "standard-high-codex", "intelligence_class": "standard-high", "pin": True,
    })
    assert result.get("success") is True, result
    assert (await db.get_task(result["task_id"])).provider_intent == PINNED


# -- pin permission -------------------------------------------------------------


async def test_worker_token_may_not_pin(setup) -> None:
    handler, db = setup
    for args in ({"pin": True}, {"provider_intent": PINNED}):
        result = await _as(handler, WORKER_SCOPE, lambda a=args: _create(
            handler, profile_id="standard-high-codex", reason="found it", **a))
        assert result["success"] is False
        assert result["code"] == "provider_intent.pin_not_permitted"
        assert next(iter(args)) in result["error"]
    assert await db.list_tasks(project_id="p") == []


async def test_elevated_supervisor_may_pin(setup) -> None:
    handler, db = setup
    result = await _as(handler, SUPERVISOR_SCOPE, lambda: _create(
        handler, profile_id="standard-high-codex", intelligence_class="standard-high", pin=True))
    assert result.get("success") is True, result
    task = await db.get_task(result["task_id"])
    assert task.provider_intent == PINNED
    audit = await db.get_task_meta(task.id, "provider_intent_audit")
    assert audit["by"] == "human:local-operator"  # no principal bound in this harness


async def _held_worker_session(db) -> None:
    await db.create_task(Task(id="held", project_id="p", title="held", description="x",
                              status=TaskStatus.IN_PROGRESS))
    import time

    await db.create_session(SessionRecord(
        id="s1", project_id="p", profile_id="standard-high-claude", harness="claude",
        provider="anthropic", name="n-s1", lifecycle="pool", work_dir="/tmp/ws", epoch="e1",
        instance_token="tok-s1", started_at=time.time(), task_id="held", state="running",
    ))


async def test_worker_filing_inherits_no_intent(setup) -> None:
    handler, db = setup
    await _held_worker_session(db)
    plain = await _as(handler, WORKER_SCOPE, lambda: _create(handler, reason="found it"))
    assert plain.get("success") is True, plain
    task = await db.get_task(plain["task_id"])
    assert task.profile_id is None and task.provider_intent == CLASS_ONLY


async def test_worker_filing_with_explicit_profile_is_preferred(setup) -> None:
    handler, db = setup
    await _held_worker_session(db)
    named = await _as(handler, WORKER_SCOPE, lambda: _create(
        handler, reason="found it", profile_id="standard-high-claude"))
    assert named.get("success") is True, named
    assert (await db.get_task(named["task_id"])).provider_intent == PREFERRED


# -- edit_task -------------------------------------------------------------------


async def _plain_task(db, task_id="t1", **fields) -> Task:
    task = Task(id=task_id, project_id="p", title="t", description="x",
                status=TaskStatus.READY, intelligence_class="standard-high", **fields)
    await db.create_task(task)
    return task


async def test_edit_profile_is_preferred_pin_is_pinned_clear_is_class_only(setup) -> None:
    handler, db = setup
    await _plain_task(db)
    result = await handler._cmd_edit_task({"task_id": "t1", "profile_id": "standard-high-codex"})
    assert "error" not in result, result
    assert (await db.get_task("t1")).provider_intent == PREFERRED
    await handler._cmd_edit_task({"task_id": "t1", "profile_id": "standard-high-codex",
                                  "pin": True})
    assert (await db.get_task("t1")).provider_intent == PINNED
    audit = await db.get_task_meta("t1", "provider_intent_audit")
    assert audit["previous"] == PREFERRED and audit["intent"] == PINNED
    await handler._cmd_edit_task({"task_id": "t1", "profile_id": None})
    task = await db.get_task("t1")
    assert task.profile_id is None and task.provider_intent == CLASS_ONLY


async def test_edit_intent_alone(setup) -> None:
    handler, db = setup
    await _plain_task(db, profile_id="standard-high-codex")
    result = await handler._cmd_edit_task({"task_id": "t1", "provider_intent": PINNED})
    assert "error" not in result, result
    assert "provider_intent" in result["fields"]
    task = await db.get_task("t1")
    assert (task.profile_id, task.provider_intent) == ("standard-high-codex", PINNED)
    await handler._cmd_edit_task({"task_id": "t1", "provider_intent": CLASS_ONLY})
    assert (await db.get_task("t1")).provider_intent == CLASS_ONLY


async def test_edit_pin_needs_a_profile(setup) -> None:
    handler, db = setup
    await _plain_task(db)
    result = await handler._cmd_edit_task({"task_id": "t1", "provider_intent": PREFERRED})
    assert result["code"] == "provider_intent.profile_required"
    assert (await db.get_task("t1")).provider_intent == CLASS_ONLY


async def test_edit_intent_rides_the_routing_guard(setup) -> None:
    handler, db = setup
    await _plain_task(db, profile_id="standard-high-codex")
    await db.update_task("t1", status=TaskStatus.IN_PROGRESS)
    result = await handler._cmd_edit_task({"task_id": "t1", "provider_intent": PINNED})
    assert "running or claimed" in result["error"]
    # ``pin: false`` alone is not a routing edit (a CLI flag pair may always send it).
    result = await handler._cmd_edit_task({"task_id": "t1", "title": "renamed", "pin": False})
    assert "error" not in result, result


async def test_worker_may_not_pin_by_edit(setup) -> None:
    handler, db = setup
    await _plain_task(db, profile_id="standard-high-codex")
    result = await _as(handler, WORKER_SCOPE, lambda: handler._cmd_edit_task(
        {"task_id": "t1", "pin": True}))
    assert result["code"] == "provider_intent.pin_not_permitted"
    assert (await db.get_task("t1")).provider_intent == CLASS_ONLY


# -- task_route ------------------------------------------------------------------


async def test_task_route_by_a_caller_is_preferred(setup) -> None:
    handler, db = setup
    await _plain_task(db)
    result = await handler._cmd_task_route({"task_id": "t1", "profile_id": "standard-high-codex"})
    assert result["success"] is True and result["provider_intent"] == PREFERRED
    assert (await db.get_task("t1")).provider_intent == PREFERRED


async def test_task_route_from_the_playbook_is_class_only(setup) -> None:
    handler, db = setup
    await _plain_task(db)
    result = await handler._cmd_task_route({"task_id": "t1", "profile_id": "standard-high-codex",
                                            "provider_intent": CLASS_ONLY})
    assert result["success"] is True
    assert (await db.get_task("t1")).provider_intent == CLASS_ONLY
    # A routed class_only row is the default: no audit write.
    assert await db.get_task_meta("t1", "provider_intent_audit") is None


async def test_task_route_never_downgrades_on_the_same_provider(setup) -> None:
    handler, db = setup
    await _plain_task(db, profile_id="standard-high-claude", provider_intent=PREFERRED)
    await handler._cmd_task_route({"task_id": "t1", "profile_id": "standard-high-claude",
                                   "provider_intent": CLASS_ONLY})
    assert (await db.get_task("t1")).provider_intent == PREFERRED
    # pinned stays pinned against a weaker intent on the same provider...
    await db.update_task_routing("t1", profile_id="standard-high-claude",
                                 intelligence_class=None, preferred_workspace_id=None,
                                 provider_intent=PINNED)
    result = await handler._cmd_task_route({"task_id": "t1", "profile_id": "standard-high-claude",
                                            "provider_intent": PREFERRED})
    assert result["provider_intent"] == PINNED
    # ...but a route onto another provider takes the intent it was given.
    result = await handler._cmd_task_route({"task_id": "t1", "profile_id": "standard-high-codex",
                                            "provider_intent": CLASS_ONLY})
    assert result["provider_intent"] == CLASS_ONLY
    assert (await db.get_task("t1")).provider_intent == CLASS_ONLY


async def test_task_route_pin_permission(setup) -> None:
    handler, db = setup
    await _plain_task(db)
    result = await _as(handler, WORKER_SCOPE, lambda: handler._cmd_task_route(
        {"task_id": "t1", "profile_id": "standard-high-codex", "pin": True}))
    assert result["code"] == "provider_intent.pin_not_permitted"
    result = await handler._cmd_task_route(
        {"task_id": "t1", "profile_id": "standard-high-codex", "pin": True})
    assert result["provider_intent"] == PINNED


# -- aq-graph ------------------------------------------------------------------


async def test_graph_node_intents(setup) -> None:
    handler, db = setup
    graph = {
        "version": 1,
        "parent": {"title": "Epic"},
        "nodes": [
            {"key": "named", "title": "Named", "profile": "standard-high-codex",
             "intelligence_class": "standard-high"},
            {"key": "pinned", "title": "Pinned", "profile": "standard-high-codex",
             "intelligence_class": "standard-high", "pin": True},
            {"key": "classed", "title": "Classed", "intelligence_class": "deep-high"},
        ],
    }
    result = await handler._cmd_create_task_graph({"project_id": "p", "graph": graph})
    assert "error" not in result, result
    by_title = {t.title: t for t in await db.list_tasks(project_id="p")}
    assert by_title["Named"].provider_intent == PREFERRED
    assert by_title["Pinned"].provider_intent == PINNED
    assert by_title["Classed"].profile_id == "deep-high-claude"
    assert by_title["Classed"].provider_intent == CLASS_ONLY


async def test_graph_profile_fill_in_is_preferred(setup) -> None:
    handler, db = setup
    graph = {"version": 1, "nodes": [{"key": "a", "title": "Filled"}]}
    result = await handler._cmd_create_task_graph({
        "project_id": "p", "graph": graph, "profile_id": "standard-high-codex",
        "intelligence_class": "standard-high",
    })
    assert "error" not in result, result
    task = next(t for t in await db.list_tasks(project_id="p") if t.title == "Filled")
    assert (task.profile_id, task.provider_intent) == ("standard-high-codex", PREFERRED)


async def test_graph_pin_without_profile_is_an_error(setup) -> None:
    handler, db = setup
    graph = {"version": 1, "nodes": [
        {"key": "a", "title": "No profile", "pin": True},
        {"key": "b", "title": "Class matched", "intelligence_class": "deep-high", "pin": True},
    ]}
    result = await handler._cmd_create_task_graph({"project_id": "p", "graph": graph})
    rules = sorted((e["rule"], e["node"]) for e in result["errors"])
    assert rules == [("pin_without_profile", "a"), ("pin_without_profile", "b")]
    assert await db.list_tasks(project_id="p") == []


async def test_graph_pin_must_be_a_boolean(setup) -> None:
    handler, _db = setup
    graph = {"version": 1, "nodes": [
        {"key": "a", "title": "A", "profile": "standard-high-codex", "pin": "yes"}]}
    result = await handler._cmd_create_task_graph({"project_id": "p", "graph": graph})
    assert any(e["rule"] == "bad_field_type" for e in result["errors"]), result


async def test_graph_still_refuses_provider_harness_and_model(setup) -> None:
    handler, _db = setup
    graph = {"version": 1, "nodes": [{"key": "a", "title": "A", "provider": "openai"}]}
    result = await handler._cmd_create_task_graph({"project_id": "p", "graph": graph})
    assert any(e["rule"] == "unsupported_routing" for e in result["errors"]), result


async def test_worker_inline_graph_may_not_pin(setup) -> None:
    handler, db = setup
    graph = {"version": 1, "nodes": [
        {"key": "a", "title": "A", "profile": "standard-high-codex", "pin": True}]}
    result = await _as(handler, WORKER_SCOPE, lambda: handler._cmd_create_task_graph(
        {"project_id": "p", "graph": graph}))
    assert result["code"] == "provider_intent.pin_not_permitted"
    assert await db.list_tasks(project_id="p") == []


def test_unpinned_nodes_serialise_exactly_as_before() -> None:
    from src.task_graph import parse_graph

    graph = parse_graph({"version": 1, "nodes": [
        {"key": "a", "title": "A", "profile": "x"},
        {"key": "b", "title": "B", "profile": "x", "pin": True}]})
    a, b = (node.to_dict() for node in graph.nodes)
    assert "pin" not in a and b["pin"] is True


FORMULA = """---
name: pinned-work
description: A formula that pins its node
---
# Pinned work

```aq-graph
version: 1
parent:
  title: Pinned work
nodes:
  - key: art
    title: Art pass
    profile: astra-high-codex
    pin: true
```
"""


async def test_vault_formula_pin_is_honoured(tmp_path) -> None:
    db = Database(lease_dsn("provider_intent_formula.db"))
    await db.initialize()
    try:
        await db.create_project(Project(id="p1", name="test"))
        await db.create_profile(_worker("astra-high-codex", "codex", "astra-high"))
        vault_root = tmp_path / "vault"
        (vault_root / "formulas").mkdir(parents=True)
        (vault_root / "formulas" / "pinned-work.md").write_text(FORMULA, encoding="utf-8")
        registry = FormulaRegistry()
        assert load_from_vault(registry, str(vault_root)) == []
        orch = MagicMock()
        orch.db = db
        orch._emit_notify = AsyncMock()
        orch.bus.emit = AsyncMock()
        orch.formula_registry = registry
        config = MagicMock()
        config.vault_root = str(vault_root)
        handler = CommandHandler(orch, config)
        handler._active_project_id = None
        result = await handler._cmd_formula_cook({"name": "pinned-work", "project_id": "p1"})
        assert result.get("success") is True, result
        art = next(t for t in await db.list_tasks(project_id="p1") if t.title == "Art pass")
        assert (art.profile_id, art.provider_intent) == ("astra-high-codex", PINNED)
    finally:
        await db.close()


# -- playbook agent_task step ------------------------------------------------------


class _CreateArgs(CommandArgs):
    title: str
    project_id: str | None = None
    profile_id: str | None = None
    pin: bool | None = None


class _CreateResult(CommandValue):
    task_id: str


_CREATE = CommandContract(
    execution=ExecutionContract(
        name="create_task",
        args_model=_CreateArgs,
        result_model=_CreateResult,
        outcomes=(
            OutcomeSpec(name="created", classification=OutcomeClass.SUCCESS),
            OutcomeSpec(name="rejected", classification=OutcomeClass.FAILURE),
        ),
        capability="create_task",
        side_effect=SideEffectClass.CREATE,
        idempotency=IdempotencySpec(mode="none"),
        retry_safe=False,
        receipt_projection=("task_id",),
    ),
    presentation=CommandPresentation(title="Create a task", summary="Create the child task"),
)


@pytest.mark.parametrize(("pin_provider", "expected"), [(None, None), (True, True)])
async def test_agent_task_pin_provider_reaches_create_task(pin_provider, expected) -> None:
    from tests.fixtures.contracts.engine_contracts import registry_with
    from tests.test_agent_task_executor import (
        StubDatabase,
        StubProfile,
        agent_task_step,
        context,
        parent_principal,
        run,
    )

    registry, adapter = registry_with(_CREATE)
    adapter.queue.append(
        CommandResult(outcome="created", value=_CreateResult(task_id="child"), summary="ok")
    )
    overrides: dict[str, Any] = {"wait_for_completion": False}
    if pin_provider is not None:
        overrides["pin_provider"] = pin_provider
    step = agent_task_step(**overrides)
    ctx = context(registry, principal=parent_principal(), db=StubDatabase({
        "reviewer": StubProfile()}))
    result = await run(step, ctx)
    assert result.outcome == "dispatched"
    (_name, args, _principal) = adapter.calls[0]
    assert args.profile_id == "reviewer"
    assert args.pin is expected


def test_absent_pin_provider_leaves_artifact_bytes_unchanged() -> None:
    from tests.test_agent_task_executor import agent_task_artifact, agent_task_step

    assert b"pin_provider" not in canonical_bytes(agent_task_artifact(agent_task_step()))
    assert b"pin_provider" in canonical_bytes(
        agent_task_artifact(agent_task_step(pin_provider=True))
    )


# -- explicit_route ------------------------------------------------------------------


def test_explicit_route_names_a_vendor_only_for_a_pin() -> None:
    base = {"id": "t", "project_id": "p", "title": "", "description": "",
            "profile_id": "standard-high-codex", "intelligence_class": "standard-high"}
    pinned = explicit_route(Task(**base, provider_intent=PINNED), vendor="openai")
    assert pinned.provider == "openai"
    for intent in (PREFERRED, CLASS_ONLY):
        assert explicit_route(Task(**base, provider_intent=intent), vendor="openai").provider is None


async def test_explicit_routing_reads_the_pinned_profiles_vendor(setup) -> None:
    _handler, db = setup
    routing = ExplicitRouting(db_getter=lambda: db)
    tasks = [
        Task(id="a", project_id="p", title="", description="", profile_id="standard-high-codex",
             intelligence_class="standard-high", provider_intent=PINNED),
        Task(id="b", project_id="p", title="", description="", profile_id="standard-high-claude",
             intelligence_class="standard-high", provider_intent=PREFERRED),
    ]
    routes = await routing.routes_for(tasks)
    assert routes["a"].provider == "openai"
    assert routes["b"].provider is None
    # Without a store every route stays provider-free, as before.
    assert (await ExplicitRouting().routes_for(tasks))["a"].provider is None


# -- delete_profile (D17) -----------------------------------------------------------


async def test_delete_profile_resets_intent_and_the_reroute_marker(setup) -> None:
    _handler, db = setup
    await db.create_profile(_worker("doomed", "codex", "standard-high"))
    await _plain_task(db, "on-doomed", profile_id="doomed", provider_intent=PINNED)
    await _plain_task(db, "moved-off-doomed", profile_id="standard-high-claude",
                      provider_intent=PREFERRED, rerouted_from="doomed")
    await _plain_task(db, "elsewhere", profile_id="standard-high-claude",
                      provider_intent=PREFERRED, rerouted_from="standard-high-codex")
    await db.delete_profile("doomed")
    on = await db.get_task("on-doomed")
    assert (on.profile_id, on.provider_intent) == (None, CLASS_ONLY)
    moved = await db.get_task("moved-off-doomed")
    assert (moved.profile_id, moved.provider_intent, moved.rerouted_from) == (
        "standard-high-claude", PREFERRED, None)
    other = await db.get_task("elsewhere")
    assert other.rerouted_from == "standard-high-codex"


# -- migration a00000000013 (D10) ------------------------------------------------------


def _load_migration():
    path = REPO_ROOT / "migrations" / "versions" / "a00000000013_provider_intent_and_reroutes.py"
    spec = importlib.util.spec_from_file_location("a00000000013", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_intent_constraints_are_named(setup) -> None:
    _handler, db = setup
    async with db._engine.connect() as conn:
        names = set((await conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE conname IN "
            "('ck_tasks_provider_intent', 'ck_archived_tasks_provider_intent', "
            "'ck_task_reroutes_reason_code')"
        ))).scalars())
    assert names == {"ck_tasks_provider_intent", "ck_archived_tasks_provider_intent",
                     "ck_task_reroutes_reason_code"}
    await _plain_task(db, "t1")
    with pytest.raises(Exception, match="ck_tasks_provider_intent"):
        async with db._engine.begin() as conn:
            await conn.execute(text("UPDATE tasks SET provider_intent = 'sticky' WHERE id = 't1'"))


async def test_migration_backfills_preferred_never_pinned_and_is_idempotent(setup) -> None:
    _handler, db = setup
    await _plain_task(db, "routed", profile_id="standard-high-codex")
    await _plain_task(db, "unrouted")
    await _plain_task(db, "archived", profile_id="standard-high-claude")
    await db.update_task("archived", status=TaskStatus.COMPLETED)
    await db.archive_task("archived")
    migration = _load_migration()

    async with db._engine.begin() as conn:
        # Rewind to the pre-revision shape: no intent, no marker, no history.
        await conn.execute(text("DROP TABLE task_reroutes"))
        for table in ("tasks", "archived_tasks"):
            await conn.execute(text(
                f"ALTER TABLE {table} DROP COLUMN provider_intent, DROP COLUMN rerouted_from"))

        def _upgrade_twice(sync_conn) -> None:
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            with Operations.context(MigrationContext.configure(sync_conn)):
                migration.upgrade()
                migration.upgrade()

        await conn.run_sync(_upgrade_twice)

    async with db._engine.connect() as conn:
        rows = dict((await conn.execute(text(
            "SELECT id, provider_intent FROM tasks"))).all())
        archived = dict((await conn.execute(text(
            "SELECT id, provider_intent FROM archived_tasks"))).all())
        indexes = set((await conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'tasks'"))).scalars())
        tables = set((await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"))).scalars())
    assert rows == {"routed": PREFERRED, "unrouted": CLASS_ONLY}
    assert archived == {"archived": PREFERRED}
    assert PINNED not in set(rows.values()) | set(archived.values())
    assert "idx_tasks_rerouted" in indexes
    assert "task_reroutes" in tables


async def test_archive_mirrors_intent_and_marker(setup) -> None:
    _handler, db = setup
    await _plain_task(db, "done", profile_id="standard-high-claude", provider_intent=PINNED,
                      rerouted_from="standard-high-codex")
    await db.update_task("done", status=TaskStatus.COMPLETED)
    await db.archive_task("done")
    archived = await db.get_archived_task("done")
    assert archived["provider_intent"] == PINNED
    assert archived["rerouted_from"] == "standard-high-codex"
