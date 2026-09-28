"""``task_route_plan`` and ``task_route_apply`` through a real handler.

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md``
§6.1-§6.6.  The planner itself is covered without I/O in
``tests/test_routing_planner.py``; this file covers what needs the
database: the snapshot the command builds, the bound-router refusal, the
advisory lock and the re-selection a burst of applies makes on fresh load.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.contracts import CONTRACTS
from src.commands.routing_commands import NOT_ROUTER
from src.config import AppConfig, DatabaseConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, Project, Task, TaskStatus, TaskType
from src.orchestrator import Orchestrator
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import granted_aq_commands
from src.playbooks.invocation import _invocation_context, current_invocation
from src.routing.sources import LEGACY, ROUTER, UNROUTED
from src.sessions.harness_parser import Harness
from tests.db_fixtures import lease_dsn
from tests.test_routing_planner import SHIPPED_POLICY

ROUTER_ID = "default-assignment-routing"
CLASSES = {
    class_id: IntelligenceClass(class_id, class_id, "", {
        "anthropic": {"model": f"claude-{class_id}"},
        "codex": {"model": f"gpt-{class_id}"},
    })
    for class_id in ("standard-high", "deep-high")
}


def _profiles() -> list[AgentProfile]:
    return [
        AgentProfile(id="standard-high-claude", name="", lifecycle="pool", harness="claude",
                     default_class="standard-high", max_active=2),
        AgentProfile(id="standard-high-codex", name="", lifecycle="pool", harness="codex",
                     default_class="standard-high", max_active=2),
        AgentProfile(id="deep-high-claude", name="", lifecycle="pool", harness="claude",
                     default_class="deep-high", max_active=1),
        AgentProfile(id="deep-high-codex", name="", lifecycle="pool", harness="codex",
                     default_class="deep-high", max_active=1),
        AgentProfile(id="reviewer", name="", harness="claude", default_class="deep-high"),
    ]


@pytest.fixture
async def orch(tmp_path):
    db = Database(lease_dsn("router.db"))
    await db.initialize()
    for profile in _profiles():
        await db.create_profile(profile)
    await db.create_project(Project(id="p", name="Project"))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database=DatabaseConfig(url=lease_dsn("unused.db")),
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
    kw.setdefault("status", TaskStatus.READY)
    await db.create_task(Task(id=task_id, project_id="p", title=task_id, description="d", **kw))
    return await db.get_task(task_id)


def _ref(playbook_id: str) -> ArtifactRef:
    return ArtifactRef(
        playbook_id=playbook_id,
        artifact_sha256="sha256:" + "a" * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test",
    )


@contextmanager
def _as_playbook(playbook_id: str = ROUTER_ID, commands=("task_route_plan", "task_route_apply")):
    """Run the body as a live command step of *playbook_id*'s artifact."""
    artifact = SimpleNamespace(steps={
        f"step_{index}": SimpleNamespace(command=command, tool_use=None)
        for index, command in enumerate(commands)
    })
    ctx = SimpleNamespace(
        run_id="run-1", dispatch_id="dispatch-1", artifact_ref=_ref(playbook_id),
        artifact=artifact, rule_id="route-task", step_id="apply_a", attempt=1,
    )
    with _invocation_context(ctx) as invocation:
        yield invocation


async def _plan(handler, task_id: str, classification=None) -> dict:
    args = {"task_id": task_id, "policy": SHIPPED_POLICY}
    if classification is not None:
        args["classification"] = classification
    return await handler.execute("task_route_plan", args)


# -- task_route_plan -------------------------------------------------------------


async def test_plan_reads_the_live_catalog_and_writes_nothing(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    assert plan["success"] and plan["outcome"] == "planned", plan
    assert plan["profile_id"] == "standard-high-codex"  # tie_order: Codex first
    assert [c["profile_id"] for c in plan["candidates"]] == [
        "standard-high-codex", "standard-high-claude",
    ]
    assert plan["policy_sha256"].startswith("sha256:")
    task = await orch.db.get_task("t")
    assert (task.profile_id, task.route_source) == (None, UNROUTED)


async def test_plan_rejects_an_invalid_policy_and_an_unknown_task(handler, orch):
    await _create(orch.db, "t")
    bad = await handler.execute("task_route_plan", {"task_id": "t", "policy": "version: 7"})
    assert bad["success"] is False and bad["code"] == "routing.invalid_policy"
    missing = await _plan(handler, "nope")
    assert missing["success"] is False


async def test_plan_reports_a_router_route_as_already_routed(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    await _route(handler, "t")
    again = await _plan(handler, "t")
    assert again["outcome"] == "already_routed"
    assert again["route_source"] == ROUTER


async def test_plan_holds_when_every_candidate_provider_is_out(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    availability = orch.provider_availability
    orch.provider_availability = SimpleNamespace(
        provider_for_profile=availability.provider_for_profile,
        effective_state=lambda key: "exhausted",
        suppresses=lambda key: True,
    )
    held = await _plan(handler, "t")
    assert held["outcome"] == "held"
    assert held["providers"] == ["claude", "codex"]


async def test_plan_honours_exclude_providers_from_the_route_constraints(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH,
                  route={"constraints": {"exclude_providers": ["codex"]}})
    plan = await _plan(handler, "t")
    assert plan["profile_id"] == "standard-high-claude"


# -- task_route_apply: only the bound router ----------------------------------------


async def test_apply_refuses_a_caller_with_no_playbook_invocation(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    assert current_invocation() is None
    refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["success"] is False and refused["code"] == NOT_ROUTER
    assert (await orch.db.get_task("t")).route_source == UNROUTED


async def test_apply_refuses_a_playbook_that_is_not_the_bound_router(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    with _as_playbook("some-other-playbook"):
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["code"] == NOT_ROUTER
    assert "not the project's bound router" in refused["error"]
    # The bound router's id on an artifact that does not grant the command.
    with _as_playbook(ROUTER_ID, commands=("task_route_plan",)):
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["code"] == NOT_ROUTER
    assert "does not grant" in refused["error"]
    # A project re-bound elsewhere refuses the old router too.
    await orch.db.update_project("p", assignment_playbook_id="project-router")
    with _as_playbook(ROUTER_ID):
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert refused["code"] == NOT_ROUTER
    assert (await orch.db.get_task("t")).route_source == UNROUTED


async def test_the_typed_contract_maps_not_router_to_rejected(handler, orch, monkeypatch):
    from src.commands.contracts import builtin
    from src.commands.principal import ExecutionPrincipal

    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    registration = CONTRACTS.get("task_route_apply")
    args = registration.contract.execution.args_model(task_id="t", plan=plan)
    result = await registration.invoke(args, ExecutionPrincipal.service("playbook-dispatch"))
    assert result.outcome == "rejected"
    assert result.summary.startswith("task_route_apply refused")


def test_granted_aq_commands_reads_command_steps_and_tool_use() -> None:
    artifact = SimpleNamespace(steps={
        "a": SimpleNamespace(command="task_route_plan", tool_use=None),
        "b": SimpleNamespace(command=None, tool_use=SimpleNamespace(aq_commands=["get_task"])),
    })
    assert granted_aq_commands(artifact) == {"task_route_plan", "get_task"}
    assert granted_aq_commands(None) == frozenset()


# -- task_route_apply: the write ------------------------------------------------------


async def _route(handler, task_id: str, classification=None) -> dict:
    plan = await _plan(handler, task_id, classification)
    assert plan["outcome"] == "planned", plan
    with _as_playbook():
        return await handler.execute("task_route_apply", {"task_id": task_id, "plan": plan})


async def test_apply_writes_the_route_resolves_the_gate_and_emits_task_routed(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, class_hint="standard-high")
    gate_id, _ = await orch.db.create_gate("p", "routing", "route me", waiter_task_ids=["t"])
    applied = await _route(handler, "t")
    assert applied["success"] and applied["outcome"] == "routed", applied
    assert applied["resolved_gate_ids"] == [gate_id]
    assert applied["adjusted_at_apply"] is False

    task = await orch.db.get_task("t")
    assert (task.profile_id, task.intelligence_class) == ("standard-high-codex", "standard-high")
    assert (task.route_source, task.provider_intent) == (ROUTER, "class_only")
    route = task.route
    assert route["rule"] == "kinds.research"
    assert route["hints"] == {"class_hint": "standard-high", "task_type": "research"}
    assert route["run_id"] == "run-1" and route["playbook_id"] == ROUTER_ID
    assert route["policy_sha256"].startswith("sha256:")
    assert {c["profile_id"] for c in route["candidates"]} == {
        "standard-high-codex", "standard-high-claude",
    }
    assert route["scores"] and route["reason"]

    emitted = [c for c in orch.bus.emit.await_args_list if c.args[0] == "task.routed"]
    assert len(emitted) == 1
    payload = emitted[0].args[1]
    assert payload["profile_id"] == "standard-high-codex"
    assert payload["provider"] == "codex" and payload["run_id"] == "run-1"
    from src.event_schemas import EVENT_SCHEMAS

    schema = EVENT_SCHEMAS["task.routed"]
    assert set(schema["required"]) <= set(payload)
    assert set(payload) <= set(schema["required"]) | set(schema["optional"])


async def test_apply_writes_the_classified_kind_and_keeps_constraints(handler, orch):
    await _create(orch.db, "t", route={"constraints": {"exclude_providers": ["claude"]}})
    classification = {
        "task_type": "research", "intelligence_class": "deep-high", "narrow": False,
        "test_verified": False, "independent_verifier": False, "reason": "open question",
    }
    applied = await _route(handler, "t", classification)
    assert applied["profile_id"] == "deep-high-codex"
    task = await orch.db.get_task("t")
    assert task.task_type == TaskType.RESEARCH
    assert task.route["constraints"] == {"exclude_providers": ["claude"]}
    assert task.route["classification"]["intelligence_class"] == "deep-high"


async def test_apply_reroutes_a_legacy_route(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH, profile_id="standard-high-claude",
                  intelligence_class="standard-high")
    assert (await orch.db.get_task("t")).route_source == LEGACY
    applied = await _route(handler, "t")
    assert applied["outcome"] == "routed"
    assert (await orch.db.get_task("t")).route_source == ROUTER


async def test_apply_is_stale_for_a_claimed_assigned_or_routed_task(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "t")
    await orch.db.create_agent(Agent(id="agent-1", name="a", profile_id="standard-high-codex"))
    await orch.db.update_task("t", assigned_agent_id="agent-1")
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "t", "plan": plan})
    assert (stale["outcome"], stale["reason"]) == ("stale", "the task is assigned")

    await _create(orch.db, "u", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "u")
    assert (await _route(handler, "u"))["outcome"] == "routed"
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "u", "plan": plan})
    assert stale["outcome"] == "stale" and "already routed" in stale["reason"]

    await _create(orch.db, "v", task_type=TaskType.RESEARCH)
    plan = await _plan(handler, "v")
    await orch.db.update_task("v", status=TaskStatus.COMPLETED)
    with _as_playbook():
        stale = await handler.execute("task_route_apply", {"task_id": "v", "plan": plan})
    assert stale["outcome"] == "stale" and "COMPLETED" in stale["reason"]


async def test_apply_refuses_a_malformed_plan(handler, orch):
    await _create(orch.db, "t", task_type=TaskType.RESEARCH)
    with _as_playbook():
        refused = await handler.execute("task_route_apply", {"task_id": "t", "plan": {}})
    assert refused["success"] is False and refused["code"] == "routing.invalid_plan"


# -- the burst ---------------------------------------------------------------------------


@pytest.mark.parametrize("concurrent", [False, True])
async def test_a_burst_planned_against_one_snapshot_spreads_by_pressure(
    handler, orch, concurrent
):
    ids = [f"t{index}" for index in range(6)]
    for task_id in ids:
        await _create(orch.db, task_id, task_type=TaskType.RESEARCH)
    # Every plan sees the same empty fleet and names the same profile.
    plans = [await _plan(handler, task_id) for task_id in ids]
    assert {plan["profile_id"] for plan in plans} == {"standard-high-codex"}

    async def apply(task_id, plan):
        return await handler.execute("task_route_apply", {"task_id": task_id, "plan": plan})

    with _as_playbook():
        if concurrent:
            results = await asyncio.gather(*(apply(t, p) for t, p in zip(ids, plans)))
        else:
            results = [await apply(t, p) for t, p in zip(ids, plans)]
    assert all(result["outcome"] == "routed" for result in results), results
    routed = [(await orch.db.get_task(task_id)).profile_id for task_id in ids]
    # Applied one at a time on fresh load, they fill both pools evenly.
    assert routed.count("standard-high-codex") == routed.count("standard-high-claude") == 3
    assert any(result["adjusted_at_apply"] for result in results)
    adjusted = next(result for result in results if result["adjusted_at_apply"])
    assert "adjusted at apply" in adjusted["reason"]
    backlog = await orch.db.count_routed_backlog_by_profile()
    assert backlog == {"standard-high-codex": 3, "standard-high-claude": 3}


# -- the routing lock and the load queries -------------------------------------------------


async def test_the_routing_lock_is_exclusive_and_bounded(orch):
    from src.database.queries.routing_queries import RoutingBusyError

    async with orch.db.routing_apply_lock():
        with pytest.raises(RoutingBusyError):
            async with orch.db.routing_apply_lock(budget_seconds=0.2):
                pass
    async with orch.db.routing_apply_lock(budget_seconds=0.2):
        pass


async def test_backlog_counts_ready_and_assigned_routed_work_fleet_wide(orch):
    await orch.db.create_project(Project(id="q", name="Other"))
    await _create(orch.db, "ready", profile_id="standard-high-codex")
    await _create(orch.db, "assigned", profile_id="standard-high-codex",
                  status=TaskStatus.ASSIGNED)
    await orch.db.create_task(Task(id="elsewhere", project_id="q", title="x", description="d",
                                   status=TaskStatus.READY, profile_id="standard-high-claude"))
    await _create(orch.db, "blocked", profile_id="standard-high-claude",
                  status=TaskStatus.BLOCKED)
    await _create(orch.db, "unrouted")
    assert await orch.db.count_routed_backlog_by_profile() == {
        "standard-high-codex": 2, "standard-high-claude": 1,
    }
    assert await orch.db.count_busy_sessions_by_profile() == {}


# -- task.route_needed carries the router and the class hint ------------------------------


async def test_route_needed_carries_the_bound_router_and_the_class_hint(orch):
    await _create(orch.db, "t", class_hint="deep-high")
    orch._route_needed_emitted = {}
    assert await orch._emit_route_needed_events() == 1
    payload = next(
        call.args[1] for call in orch.bus.emit.await_args_list
        if call.args[0] == "task.route_needed"
    )
    assert payload["router"] == ROUTER_ID
    assert payload["class_hint"] == "deep-high"
