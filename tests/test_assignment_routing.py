"""Assignment routing after the playbook cutover: the task row is the route.

Spec: docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md.
The orchestrator reads ``tasks.intelligence_class`` and emits
``task.route_needed`` for anything missing a class or a profile; the
``default-assignment-routing`` playbook does the deciding through
``task_route_options`` and ``task_route``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.assignment_routing import EffectiveAssignmentRoute, explicit_route, explicit_routes
from src.commands.routing_commands import build_route_options, profile_for_class
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, AgentState, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.orchestrator.route_needed import ROUTE_NEEDED_INTERVAL_SECONDS
from src.sessions.harness_parser import Harness

CLASSES = {
    "standard-medium": IntelligenceClass("standard-medium", "Standard", "", {
        "anthropic": {"model": "claude-sonnet-5"},
        "codex": {"model": "gpt-5.6-sol"},
    }),
    "deep-low": IntelligenceClass("deep-low", "Deep", "", {
        "anthropic": {"model": "claude-fable-5"},
        "codex": {"model": "gpt-5.6-sol"},
    }),
    "fast-low": IntelligenceClass("fast-low", "Fast", "", {
        "anthropic": {"model": "claude-haiku-4-5"},
    }),
}


def _task(**changes) -> Task:
    values = {
        "id": "task-1", "project_id": "p", "title": "Fix flaky checkout",
        "description": "Find and repair the checkout race.", "priority": 20,
    }
    values.update(changes)
    return Task(**values)


# -- explicit routes ---------------------------------------------------------


def test_explicit_route_is_the_task_class_or_nothing() -> None:
    assert explicit_route(_task()) is None
    assert explicit_route(_task(intelligence_class="  ")) is None
    route = explicit_route(_task(intelligence_class="deep-low"))
    assert route == EffectiveAssignmentRoute("task-1", "deep-low", None, "explicit")
    routes = explicit_routes([_task(), _task(id="t2", intelligence_class="fast-low")])
    assert set(routes) == {"t2"}


# -- the option catalog and the deterministic profile pick --------------------


def _profiles():
    return [
        AgentProfile(id="standard-medium-claude", name="", lifecycle="pool",
                     harness="claude", default_class="standard-medium", max_active=4),
        AgentProfile(id="deep-low-codex", name="", lifecycle="pool",
                     harness="codex", default_class="deep-low", max_active=2),
        AgentProfile(id="deep-low-claude", name="", lifecycle="pool",
                     harness="claude", default_class="deep-low", max_active=2),
        AgentProfile(id="worker-generic", name="", lifecycle="task", harness="claude"),
        AgentProfile(id="pool-no-class", name="", lifecycle="pool", harness="claude"),
        AgentProfile(id="reviewer", name="", harness="claude", default_class="deep-low"),
        AgentProfile(id="project:old:thing", name="", harness="claude", default_class="deep-low"),
    ]


def _registry():
    from src.sessions.harness_registry import HarnessRegistry

    registry = HarnessRegistry()
    for harness in ("claude", "codex"):
        registry.upsert(Harness(id=harness, name=harness, command=harness, model_flag="--model"))
    return registry


def test_build_route_options_is_one_row_per_class_provider_profile() -> None:
    agents = [Agent(id="a1", name="a", profile_id="worker-generic", state=AgentState.IDLE)]
    rows = build_route_options("p", _profiles(), agents, _registry(), CLASSES)
    keyed = {(r["intelligence_class"], r["provider"], r["profile_id"]): r for r in rows}
    assert ("deep-low", "anthropic", "deep-low-claude") in keyed
    assert ("deep-low", "openai", "deep-low-codex") in keyed
    assert ("standard-medium", "anthropic", "standard-medium-claude") in keyed
    # a generic task-lifecycle profile offers every class its provider maps
    assert ("fast-low", "anthropic", "worker-generic") in keyed
    assert keyed[("fast-low", "anthropic", "worker-generic")]["idle_count"] == 1
    # control profiles, retired ids and class-less pools offer nothing
    assert not [r for r in rows if r["profile_id"] in {"reviewer", "project:old:thing", "pool-no-class"}]
    assert keyed[("deep-low", "anthropic", "deep-low-claude")]["configured_capacity"] == 2
    assert rows == sorted(rows, key=lambda r: (r["intelligence_class"], r["provider"], r["profile_id"]))


def test_profile_for_class_prefers_pin_then_pool_on_default_provider_then_id() -> None:
    rows = build_route_options("p", _profiles(), [], _registry(), CLASSES)
    assert profile_for_class(rows, "deep-low", prefer_provider="anthropic") == "deep-low-claude"
    assert profile_for_class(rows, "deep-low", prefer_provider="openai") == "deep-low-codex"
    assert profile_for_class(rows, "deep-low") == "deep-low-claude"
    assert profile_for_class(rows, "deep-low", pinned_profile_id="deep-low-codex") == "deep-low-codex"
    # a pin that cannot serve the class is ignored
    assert profile_for_class(rows, "deep-low", pinned_profile_id="standard-medium-claude",
                             prefer_provider="anthropic") == "deep-low-claude"
    # pools beat generic task profiles; only the generic one runs fast-low
    assert profile_for_class(rows, "fast-low") == "worker-generic"
    assert profile_for_class(rows, "nope") is None


# -- task_route_options + task_route through a real handler ------------------


@pytest.fixture
async def orch(tmp_path):
    db = Database(str(tmp_path / "routing.db"))
    await db.initialize()
    for profile in _profiles():
        if ":" in profile.id:
            continue
        await db.create_profile(profile)
    await db.create_project(
        Project(id="p", name="Project", default_profile_id="standard-medium-claude")
    )
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "unused.db"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orchestrator = Orchestrator(cfg)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.bus.emit = AsyncMock()
    for harness in ("claude", "codex"):
        orchestrator.harness_registry.upsert(Harness(
            id=harness, name=harness, command=harness, model_flag="--model",
        ))
    orchestrator.session_spec_builder._intelligence_classes = dict(CLASSES)
    orchestrator.intelligence_classes.replace(dict(CLASSES))
    yield orchestrator
    await db.close()


@pytest.fixture
def handler(orch):
    from src.commands.handler import CommandHandler

    return CommandHandler(orch, orch.config)


async def _create(db, task_id: str, **kw) -> Task:
    await db.create_task(Task(
        id=task_id, project_id="p", title=task_id, description="d",
        status=TaskStatus.READY, **kw,
    ))
    return await db.get_task(task_id)


async def test_route_options_undecided_when_no_class(handler, orch):
    await _create(orch.db, "t")
    res = await handler._cmd_task_route_options({"task_id": "t"})
    assert res["success"] and res["outcome"] == "undecided"
    assert res["intelligence_class"] is None and res["explicit_profile_id"] is None
    assert res["default_profile_id"] == "standard-medium-claude"
    assert {r["profile_id"] for r in res["options"]} >= {"deep-low-claude", "standard-medium-claude"}


async def test_route_options_explicit_names_the_serving_profile(handler, orch):
    await _create(orch.db, "t", intelligence_class="deep-low")
    res = await handler._cmd_task_route_options({"task_id": "t"})
    assert res["outcome"] == "explicit"
    assert res["explicit_profile_id"] == "deep-low-claude"


async def test_route_options_already_routed_and_no_options(handler, orch):
    await _create(orch.db, "done", intelligence_class="deep-low", profile_id="deep-low-codex")
    res = await handler._cmd_task_route_options({"task_id": "done"})
    assert res["outcome"] == "already_routed" and res["explicit_profile_id"] == "deep-low-codex"

    await _create(orch.db, "orphan", intelligence_class="spark-ultra")
    res = await handler._cmd_task_route_options({"task_id": "orphan"})
    assert res["outcome"] == "no_options"

    res = await handler._cmd_task_route_options({"task_id": "missing"})
    assert res["success"] is False


async def test_task_route_writes_class_profile_and_reason(handler, orch):
    await _create(orch.db, "t")
    res = await handler._cmd_task_route({
        "task_id": "t", "profile_id": "deep-low-claude",
        "intelligence_class": "deep-low", "reason": "hard problem",
    })
    assert res["success"], res
    task = await orch.db.get_task("t")
    assert (task.intelligence_class, task.profile_id) == ("deep-low", "deep-low-claude")
    explained = await handler._cmd_explain_task({"task_id": "t"})
    assert explained["assignment_route"]["reason"] == "hard problem"
    assert explained["assignment_route"]["source"] == "explicit"
    # the pool that runs the class now sees the demand
    assert await orch.db.count_ready_by_profile("p") == {"deep-low-claude": 1}


# -- the cascade's only routing job: emit task.route_needed ------------------


async def test_route_needed_is_emitted_once_per_interval_for_unrouted_work(orch):
    await _create(orch.db, "no-class")
    await _create(orch.db, "class-only", intelligence_class="deep-low")
    await _create(orch.db, "routed", intelligence_class="deep-low", profile_id="deep-low-claude")
    await orch.db.create_agent(Agent(id="agent-x", name="x", profile_id="worker-generic"))
    await _create(orch.db, "taken", assigned_agent_id="agent-x")
    events = []
    orch.bus.emit = AsyncMock(side_effect=lambda et, data=None: events.append((et, data)))

    assert await orch._emit_route_needed_events() == 2
    by_task = {d["task_id"]: d for et, d in events if et == "task.route_needed"}
    assert set(by_task) == {"no-class", "class-only"}
    assert by_task["class-only"]["intelligence_class"] == "deep-low"
    assert by_task["no-class"]["intelligence_class"] is None
    assert by_task["no-class"]["priority"] == 20 or "priority" in by_task["no-class"]

    # nothing re-emits inside the interval
    assert await orch._emit_route_needed_events() == 0
    for task_id in ("no-class", "class-only"):
        orch._route_needed_emitted[task_id] -= ROUTE_NEEDED_INTERVAL_SECONDS + 1
    assert await orch._emit_route_needed_events() == 2

    # once routed, the task drops out and its throttle entry is forgotten
    await orch.db.update_task_routing(
        "no-class", profile_id="deep-low-claude", intelligence_class="deep-low",
        preferred_workspace_id=None,
    )
    orch._route_needed_emitted["class-only"] -= ROUTE_NEEDED_INTERVAL_SECONDS + 1
    assert await orch._emit_route_needed_events() == 1
    assert "no-class" not in orch._route_needed_emitted


async def test_cycle_uses_the_task_row_as_the_route(orch):
    task = await _create(orch.db, "t", intelligence_class="deep-low")
    routes = await orch.assignment_routing.routes_for([task, await _create(orch.db, "bare")])
    assert set(routes) == {"t"} and routes["t"].source == "explicit"
