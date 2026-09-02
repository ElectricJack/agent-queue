from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.llm import LLMRunResult
from src.models import AgentProfile, Project, Task, TaskStatus
from src.orchestrator.assignment_routing import (
    AssignmentRoutingCoordinator,
    AssignmentRoutingValidationError,
    task_assignment_options,
    validate_assignment_response,
)
from src.playbooks.manager import PlaybookManager
from src.playbooks.services import PlaybookServices


DEFAULT_MARKDOWN = """---
id: default-assignment-routing
kind: assignment-routing
role: assignment-routing
scope: system
triggers: [assignment.route.requested]
---
Choose the cheapest reliable option.
"""


@pytest.fixture
async def coordinator_system(tmp_path):
    db = Database(str(tmp_path / "coordinator.db"))
    await db.initialize()
    await db.create_profile(AgentProfile(
        id="worker-fast",
        name="Fast worker",
        harness="claude",
        lifecycle="task",
        default_class="fast-low",
    ))
    await db.create_project(Project(id="p", name="P", default_profile_id="worker-fast"))
    manager = PlaybookManager(config=None)
    assert (await manager.compile_playbook(DEFAULT_MARKDOWN)).success

    services = PlaybookServices.for_tests(MagicMock())
    services.llm = MagicMock()
    services.llm.config = SimpleNamespace(max_tokens=2048)
    services.llm.run_tools = AsyncMock()
    services.llm.complete = AsyncMock()
    services.handler.set_caller_profile = MagicMock()
    services.handler.set_active_project = MagicMock()

    bus = MagicMock()
    bus.emit = AsyncMock()
    owner = SimpleNamespace(
        db=db,
        playbook_manager=manager,
        harness_registry=SimpleNamespace(
            get=lambda harness_id, project_id=None: SimpleNamespace(
                id=harness_id, command=harness_id, provider="anthropic"
            )
        ),
        session_spec_builder=SimpleNamespace(_intelligence_classes={
            "fast-low": IntelligenceClass(
                id="fast-low",
                name="Fast low",
                description="Routine work",
                mapping={"anthropic": {"model": "claude-haiku"}},
            )
        }),
        _provider_cooldowns={},
        playbook_services=lambda: services,
        _resolve_gate_and_emit=AsyncMock(),
        bus=bus,
    )
    coordinator = AssignmentRoutingCoordinator(owner)
    yield coordinator, services, db
    await db.close()


def _decision(task, intelligence_class="fast-low", provider=None):
    from src.assignment_routing import assignment_input_hash

    return {
        "task_id": task.id,
        "input_hash": assignment_input_hash(task),
        "intelligence_class": intelligence_class,
        "provider": provider,
        "reason": "Routine localized work.",
    }


def test_validator_rejects_extra_or_unsupported_decisions():
    task = Task(id="t", project_id="p", title="T", description="D", updated_at=1)
    options = [SimpleNamespace(intelligence_class="fast-low", provider="anthropic")]
    payload = {"decisions": [_decision(task), _decision(task)]}
    with pytest.raises(AssignmentRoutingValidationError, match="duplicate"):
        validate_assignment_response(json.dumps(payload), [task], options)


@pytest.mark.asyncio
async def test_pinned_profile_is_offered_and_validated_only_at_its_fixed_class(
    coordinator_system,
):
    coordinator, services, db = coordinator_system
    await db.create_profile(AgentProfile(
        id="worker-standard",
        name="Standard worker",
        harness="claude",
        lifecycle="task",
        default_class="standard-low",
    ))
    await db.create_profile(AgentProfile(
        id="reviewer",
        name="Reviewer",
        harness="claude",
        lifecycle="task",
        default_class="standard-low",
    ))
    coordinator.owner.session_spec_builder._intelligence_classes["standard-low"] = (
        IntelligenceClass(
            id="standard-low",
            name="Standard low",
            description="Review work",
            mapping={"anthropic": {"model": "claude-sonnet"}},
        )
    )
    await db.create_task(Task(
        id="review",
        project_id="p",
        title="Review the change",
        description="Check the submitted implementation.",
        profile_id="reviewer",
        status=TaskStatus.READY,
    ))
    task = await db.get_task("review")
    options, profiles = await coordinator._catalog("p")
    constrained = task_assignment_options(task, options, profiles)
    assert [option.intelligence_class for option in constrained] == ["standard-low"]

    invalid = {"decisions": [_decision(task, "fast-low")]}
    with pytest.raises(AssignmentRoutingValidationError, match="unsupported intelligence_class"):
        validate_assignment_response(json.dumps(invalid), [task], constrained)

    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(task, "standard-low")]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )
    routes = await coordinator.reconcile()

    event = json.loads((await db.list_playbook_runs())[0].trigger_event)
    assert [option["intelligence_class"] for option in event["options"]] == ["standard-low"]
    assert routes["review"].intelligence_class == "standard-low"
    saved = await db.get_task_assignment_route("review")
    assert saved.options_hash == coordinator.cached_options_hash("p")


@pytest.mark.asyncio
async def test_coordinator_batches_ready_tasks_in_one_llm_call(coordinator_system):
    coordinator, services, db = coordinator_system
    for index in range(2):
        await db.create_task(Task(
            id=f"t-{index}",
            project_id="p",
            title=f"Task {index}",
            description="Small change",
            status=TaskStatus.READY,
        ))
    tasks = await db.list_tasks(project_id="p", status=TaskStatus.READY)
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(task) for task in tasks]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )

    committed = await coordinator.reconcile()

    assert services.llm.run_tools.await_count == 1
    event = json.loads((await db.list_playbook_runs())[0].trigger_event)
    assert len(event["tasks"]) == 2
    assert set(committed) == {"t-0", "t-1"}
    saved = [await db.get_task_assignment_route(task.id) for task in tasks]
    assert all(saved)


@pytest.mark.asyncio
async def test_reconcile_is_a_noop_without_a_playbook_manager(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="paused-playbooks",
        project_id="p",
        title="Paused playbooks",
        description="The playbook subsystem is intentionally disabled.",
        status=TaskStatus.READY,
    ))
    coordinator.owner.playbook_manager = None

    assert await coordinator.reconcile() == {}
    services.llm.run_tools.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_class_bypasses_llm(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="explicit",
        project_id="p",
        title="Explicit",
        description="Already classified",
        status=TaskStatus.READY,
        intelligence_class="fast-low",
    ))

    routes = await coordinator.reconcile()

    services.llm.run_tools.assert_not_awaited()
    assert routes["explicit"].source == "explicit"
    assert await db.get_task_assignment_route("explicit") is None


@pytest.mark.asyncio
async def test_edit_during_llm_skips_only_edited_task(coordinator_system):
    coordinator, services, db = coordinator_system
    for index in range(2):
        await db.create_task(Task(
            id=f"stale-{index}", project_id="p", title=f"Task {index}",
            description="Small change", status=TaskStatus.READY,
        ))
    tasks = await db.list_tasks(project_id="p", status=TaskStatus.READY)

    async def respond(*args, **kwargs):
        await db.update_task(tasks[0].id, title="Edited while routing")
        return LLMRunResult(
            text=json.dumps({"decisions": [_decision(task) for task in tasks]}),
            transcript=[], turns=1, stopped_by="done",
        )

    services.llm.run_tools.side_effect = respond
    await coordinator.reconcile()

    assert await db.get_task_assignment_route(tasks[0].id) is None
    assert await db.get_task_assignment_route(tasks[1].id) is not None


@pytest.mark.asyncio
async def test_routes_task_blocked_only_by_legacy_routing_gate(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="legacy-gate",
        project_id="p",
        title="Legacy gated task",
        description="Route and release it",
        status=TaskStatus.READY,
    ))
    gate_id, _ = await db.create_gate(
        "p",
        "routing",
        "Legacy route",
        waiter_task_ids=["legacy-gate"],
    )
    task = await db.get_task("legacy-gate")
    assert task.is_blocked
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(task)]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )

    committed = await coordinator.reconcile()

    assert "legacy-gate" in committed
    coordinator.owner._resolve_gate_and_emit.assert_awaited_once()
    gate_call = coordinator.owner._resolve_gate_and_emit.await_args
    assert gate_call.args == (gate_id,)
    assert gate_call.kwargs["resolved_by"] == "assignment-routing"
    assert "playbook run" in gate_call.kwargs["resolution"]


@pytest.mark.asyncio
async def test_plan_subtask_is_not_an_assignment_candidate(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="plan-child",
        project_id="p",
        title="Plan child",
        description="Internal plan bookkeeping",
        status=TaskStatus.READY,
        is_plan_subtask=True,
    ))
    await db.create_task(Task(
        id="ordinary",
        project_id="p",
        title="Ordinary work",
        description="Route this",
        status=TaskStatus.READY,
    ))
    ordinary = await db.get_task("ordinary")
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(ordinary)]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )

    await coordinator.reconcile()

    event = json.loads((await db.list_playbook_runs())[0].trigger_event)
    assert [task["task_id"] for task in event["tasks"]] == ["ordinary"]
    assert await db.get_task_assignment_route("plan-child") is None


@pytest.mark.asyncio
async def test_unblocked_defined_task_is_routed_before_promotion(coordinator_system):
    """A worker files work as DEFINED; it must not wait for the cascade."""
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="filed",
        project_id="p",
        title="Follow-up found while working",
        description="File it, route it, then promote it",
        status=TaskStatus.DEFINED,
    ))
    await db.create_task(Task(
        id="upstream",
        project_id="p",
        title="Upstream work",
        description="Must finish first",
        status=TaskStatus.IN_PROGRESS,
    ))
    await db.create_task(Task(
        id="waiting",
        project_id="p",
        title="Blocked child",
        description="Still waiting on a dependency",
        status=TaskStatus.DEFINED,
    ))
    await db.add_dependency("waiting", "upstream")
    assert (await db.get_task("waiting")).is_blocked
    filed = await db.get_task("filed")
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(filed)]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )

    committed = await coordinator.reconcile()

    assert set(committed) == {"filed"}
    event = json.loads((await db.list_playbook_runs())[0].trigger_event)
    assert [task["task_id"] for task in event["tasks"]] == ["filed"]
    assert (await db.get_task_assignment_route("filed")).intelligence_class == "fast-low"
    assert await db.get_task_assignment_route("waiting") is None


@pytest.mark.asyncio
async def test_worker_filed_task_born_gated_is_routed_and_released(coordinator_system):
    """A root worker filing is DEFINED *and* holds an open routing gate.

    It used to be invisible to the coordinator, so nothing ever chose a
    class and nothing ever resolved the gate waiting on that choice — every
    filed task needed a supervisor to hand-route it.
    """
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="filed",
        project_id="p",
        title="Found a parser defect",
        description="Filed while working on the held task",
        status=TaskStatus.DEFINED,
    ))
    gate_id, _ = await db.create_gate(
        "p", "routing", "Route: Found a parser defect", waiter_task_ids=["filed"]
    )
    filed = await db.get_task("filed")
    assert filed.status == TaskStatus.DEFINED and filed.is_blocked
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(filed)]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )

    committed = await coordinator.reconcile()

    assert set(committed) == {"filed"}
    assert (await db.get_task_assignment_route("filed")).intelligence_class == "fast-low"
    coordinator.owner._resolve_gate_and_emit.assert_awaited_once()
    assert coordinator.owner._resolve_gate_and_emit.await_args.args == (gate_id,)


@pytest.mark.asyncio
async def test_revision_bump_restamps_the_route_instead_of_rerouting(coordinator_system):
    """The claim query joins on ``task_updated_at``; keep it in step."""
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="filed",
        project_id="p",
        title="Follow-up",
        description="Route once",
        status=TaskStatus.DEFINED,
    ))
    filed = await db.get_task("filed")
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(filed)]}),
        transcript=[],
        turns=1,
        stopped_by="done",
    )
    await coordinator.reconcile()
    assert services.llm.run_tools.await_count == 1

    # Promotion moves the revision without touching a single routed input.
    await db.transition_task("filed", TaskStatus.READY, context="deps_met_no_deps")
    promoted = await db.get_task("filed")
    assert promoted.updated_at != (await db.get_task_assignment_route("filed")).task_updated_at

    resolved = await coordinator.reconcile()

    assert services.llm.run_tools.await_count == 1  # no second decision
    assert resolved["filed"].intelligence_class == "fast-low"
    assert (await db.get_task_assignment_route("filed")).task_updated_at == promoted.updated_at


@pytest.mark.asyncio
async def test_invalid_response_waits_for_retry_without_fallback(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="invalid",
        project_id="p",
        title="Invalid response",
        description="Do not guess a class",
        status=TaskStatus.READY,
    ))
    services.llm.run_tools.side_effect = [
        LLMRunResult(
            text='{"decisions": []}', transcript=[], turns=1, stopped_by="done"
        ),
        LLMRunResult(
            text=json.dumps({"decisions": [_decision(await db.get_task("invalid"))]}),
            transcript=[],
            turns=1,
            stopped_by="done",
        ),
    ]

    assert await coordinator.reconcile() == {}
    task = await db.get_task("invalid")
    detail, reason = await coordinator.explain(task)
    assert detail is None
    assert reason["code"] == "assignment_route_retry"
    assert await db.get_task_assignment_route("invalid") is None

    await coordinator.reconcile()
    assert services.llm.run_tools.await_count == 1

    coordinator._retry = {
        key: (count, 0.0, error)
        for key, (count, _retry_at, error) in coordinator._retry.items()
    }
    routes = await coordinator.reconcile()

    assert routes["invalid"].intelligence_class == "fast-low"
    assert services.llm.run_tools.await_count == 2
    runs = sorted(await db.list_playbook_runs(), key=lambda run: run.started_at)
    assert [run.status for run in runs] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_catalog_change_reroutes_saved_decision(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="catalog-change",
        project_id="p",
        title="Catalog change",
        description="Route again after an administrative change",
        status=TaskStatus.READY,
    ))
    task = await db.get_task("catalog-change")
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(task)]}),
        transcript=[], turns=1, stopped_by="done",
    )
    await coordinator.reconcile()
    first = await db.get_task_assignment_route(task.id)
    assert first.intelligence_class == "fast-low"

    coordinator.owner.session_spec_builder._intelligence_classes["deep-high"] = (
        IntelligenceClass(
            id="deep-high",
            name="Deep high",
            description="Complex work",
            mapping={"anthropic": {"model": "claude-opus"}},
        )
    )
    await db.update_profile("worker-fast", default_class="deep-high")
    task = await db.get_task(task.id)
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(task, "deep-high")]}),
        transcript=[], turns=1, stopped_by="done",
    )

    await coordinator.reconcile()

    second = await db.get_task_assignment_route(task.id)
    assert second.intelligence_class == "deep-high"
    assert second.options_hash != first.options_hash
    assert services.llm.run_tools.await_count == 2


@pytest.mark.asyncio
async def test_restart_reuses_fresh_successful_decision(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="restart",
        project_id="p",
        title="Restart recovery",
        description="Reuse the saved decision",
        status=TaskStatus.READY,
    ))
    task = await db.get_task("restart")
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(task)]}),
        transcript=[], turns=1, stopped_by="done",
    )
    await coordinator.reconcile()
    services.llm.run_tools.reset_mock()

    restarted = AssignmentRoutingCoordinator(coordinator.owner)
    routes = await restarted.reconcile()

    assert routes["restart"].intelligence_class == "fast-low"
    services.llm.run_tools.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_coordinators_share_one_identical_run(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_task(Task(
        id="concurrent",
        project_id="p",
        title="Concurrent route",
        description="Deduplicate the identical batch",
        status=TaskStatus.READY,
    ))
    task = await db.get_task("concurrent")

    async def delayed_response(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return LLMRunResult(
            text=json.dumps({"decisions": [_decision(task)]}),
            transcript=[], turns=1, stopped_by="done",
        )

    services.llm.run_tools.side_effect = delayed_response
    contender = AssignmentRoutingCoordinator(coordinator.owner)

    await asyncio.gather(coordinator.reconcile(), contender.reconcile())

    assert services.llm.run_tools.await_count == 1
    assert len(await db.list_playbook_runs()) == 1
    assert await db.get_task_assignment_route(task.id) is not None


@pytest.mark.asyncio
async def test_broken_project_override_does_not_block_other_projects(coordinator_system):
    coordinator, services, db = coordinator_system
    await db.create_project(Project(
        id="a-broken",
        name="Broken override",
        default_profile_id="worker-fast",
        assignment_playbook_id="missing-project-router",
    ))
    await db.create_task(Task(
        id="broken-task",
        project_id="a-broken",
        title="Broken project task",
        description="Wait visibly",
        status=TaskStatus.READY,
    ))
    await db.create_task(Task(
        id="healthy-task",
        project_id="p",
        title="Healthy project task",
        description="This project should still route",
        status=TaskStatus.READY,
    ))
    healthy = await db.get_task("healthy-task")
    services.llm.run_tools.return_value = LLMRunResult(
        text=json.dumps({"decisions": [_decision(healthy)]}),
        transcript=[], turns=1, stopped_by="done",
    )

    routes = await coordinator.reconcile()

    assert "healthy-task" in routes
    assert await db.get_task_assignment_route("broken-task") is None
    _detail, reason = await coordinator.explain(await db.get_task("broken-task"))
    assert reason["code"] == "assignment_playbook_unavailable"
