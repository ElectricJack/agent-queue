from __future__ import annotations

import json
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
    validate_assignment_response,
)
from src.playbooks.compiler import compile_playbook
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
