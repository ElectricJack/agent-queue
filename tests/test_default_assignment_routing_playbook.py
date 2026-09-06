"""The shipped routing policy runs as an ordinary pipeline over the route commands.

Spec: ``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``.
The orchestrator only emits ``task.route_needed``; everything that decides
what a task needs — the class, the profile, the reason — is this playbook.
"""

from __future__ import annotations

from pathlib import Path

from src.commands.contracts import CONTRACTS
from src.commands.contracts.builtin import set_handler_provider
from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile, Project, Task, TaskStatus
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS, ExecutionMode
from src.playbooks.executors.base import EngineServices
from src.playbooks.executors.llm import _result as llm_result
from src.profiles.capabilities import CapabilityPolicy
from src.sessions.harness_parser import Harness
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

FIXTURE = Path("tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json")

CLASSES = {
    "standard-medium": IntelligenceClass("standard-medium", "Standard", "", {
        "anthropic": {"model": "claude-sonnet-5"},
    }),
    "deep-low": IntelligenceClass("deep-low", "Deep", "", {
        "anthropic": {"model": "claude-fable-5"},
    }),
}


class _ScriptedLlm:
    """Stands in for the live LLM executor: answers the choose step with *decision*."""

    def __init__(self, decision: dict) -> None:
        self.decision = decision
        self.prompts: list[str] = []

    async def execute(self, step, ctx):
        self.prompts.append(step.prompt.value)
        self.inputs = dict(ctx.inputs)
        return llm_result(step, ctx, outcome="completed", value=self.decision)


async def _handler(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    for profile in (
        AgentProfile(id="playbook-compiler", name="compiler", harness="claude"),
        AgentProfile(
            id="standard-medium-claude", name="std", lifecycle="pool",
            harness="claude", default_class="standard-medium",
        ),
        AgentProfile(
            id="deep-low-claude", name="deep", lifecycle="pool",
            harness="claude", default_class="deep-low",
        ),
    ):
        await db.create_profile(profile)
    await db.create_project(
        Project(id="p", name="Project", default_profile_id="standard-medium-claude")
    )
    orch = handler.orchestrator
    orch.harness_registry.upsert(
        Harness(id="claude", name="claude", command="claude", model_flag="--model")
    )
    orch.session_spec_builder._intelligence_classes = dict(CLASSES)
    orch.intelligence_classes = None  # route validation reads the builder's dict
    return handler


def _engine(handler, artifact):
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS,
            clock=lambda: 123.0,
            artifact_store=InMemoryArtifactStore({artifact.id: artifact}),
            handler=handler,
            db=handler.db,
        ),
        runs=runs,
        waits=runs,
        activations=StubActivations([artifact_ref_for(artifact)]),
    )
    return engine, runs


def _principal():
    return ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        project_id="p",
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["task_route_options", "task_route"]
        ),
    )


def _event(task: Task) -> dict:
    return {
        "event_type": "task.route_needed",
        "event_id": f"route-{task.id}",
        "task_id": task.id,
        "project_id": task.project_id,
        "title": task.title,
    }


async def test_explicit_class_is_pinned_to_the_pool_that_serves_it(
    command_handler_factory,
):
    handler = await _handler(command_handler_factory)
    await handler.db.create_task(Task(
        id="explicit", project_id="p", title="Hard bug", description="",
        status=TaskStatus.READY, intelligence_class="deep-low",
    ))
    artifact = load_definition_json(FIXTURE.read_text(encoding="utf-8"))
    engine, runs = _engine(handler, artifact)
    set_handler_provider(lambda: handler)
    try:
        result = await engine.dispatch_event(
            _event(await handler.db.get_task("explicit")), _principal()
        )
    finally:
        set_handler_provider(None)
    assert tuple(result.rules_selected) == ("route-task",)
    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    steps = [r.step_id for r in runs.receipts]
    assert "route-task--choose" not in steps
    assert "route-task--apply_explicit" in steps

    task = await handler.db.get_task("explicit")
    assert task.profile_id == "deep-low-claude"
    assert task.intelligence_class == "deep-low"
    assert await handler.db.count_ready_by_profile("p") == {"deep-low-claude": 1}


async def test_undecided_task_takes_the_llm_decision_from_the_options(
    command_handler_factory, monkeypatch,
):
    handler = await _handler(command_handler_factory)
    await handler.db.create_task(Task(
        id="open", project_id="p", title="Fix a typo", description="one-line change",
        status=TaskStatus.READY,
    ))
    scripted = _ScriptedLlm({
        "intelligence_class": "standard-medium", "provider": "anthropic",
        "profile_id": "standard-medium-claude", "reason": "routine localized edit",
    })
    monkeypatch.setitem(EXECUTORS[ExecutionMode.LIVE], "llm", scripted)
    artifact = load_definition_json(FIXTURE.read_text(encoding="utf-8"))
    engine, runs = _engine(handler, artifact)
    set_handler_provider(lambda: handler)
    try:
        result = await engine.dispatch_event(
            _event(await handler.db.get_task("open")), _principal()
        )
    finally:
        set_handler_provider(None)
    assert tuple(result.rules_selected) == ("route-task",)
    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert "Choosing a class" in scripted.prompts[0]
    assert scripted.inputs["title"] == "Fix a typo"
    rows = scripted.inputs["options"]
    assert {(r["intelligence_class"], r["profile_id"]) for r in rows} == {
        ("standard-medium", "standard-medium-claude"), ("deep-low", "deep-low-claude"),
    }

    task = await handler.db.get_task("open")
    assert task.intelligence_class == "standard-medium"
    assert task.profile_id == "standard-medium-claude"


async def test_already_routed_task_ends_without_writing(command_handler_factory):
    handler = await _handler(command_handler_factory)
    await handler.db.create_task(Task(
        id="done", project_id="p", title="Routed", description="",
        status=TaskStatus.READY, intelligence_class="deep-low", profile_id="deep-low-claude",
    ))
    before = await handler.db.get_task("done")
    artifact = load_definition_json(FIXTURE.read_text(encoding="utf-8"))
    engine, runs = _engine(handler, artifact)
    set_handler_provider(lambda: handler)
    try:
        await engine.dispatch_event(_event(before), _principal())
    finally:
        set_handler_provider(None)
    (run,) = runs.snapshots.values()
    assert run.lifecycle.value == "completed", run.error
    assert [r.step_id for r in runs.receipts if r.step_id.startswith("route-task--apply")] == []
    assert (await handler.db.get_task("done")).updated_at == before.updated_at
