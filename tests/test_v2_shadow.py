"""Side-effect-free V2 shadow execution — Package 4 C5 / T-12."""

from __future__ import annotations

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS
from src.playbooks.executors.base import EngineServices, ExecutionMode
from tests.fixtures.contracts.engine_contracts import GATE_CREATE, ENSURE_TASK, LIST_TASKS, ScriptedAdapter, registry_with
from tests.playbook_v2_engine_helpers import InMemoryArtifactStore, RecordingRunRepository, artifact_ref_for, event, load_artifact


class RaisingBus:
    async def emit(self, *_args, **_kwargs) -> None:
        raise AssertionError("shadow must not emit bus events")


def build() -> tuple[PlaybookEngine, ScriptedAdapter]:
    artifact = load_artifact("review-pipeline.artifact.json")
    store = InMemoryArtifactStore()
    store.put(artifact)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS, GATE_CREATE)
    return (
        PlaybookEngine(
            services=EngineServices(
                contracts=registry,
                clock=lambda: 1_000.0,
                artifact_store=store,
                bus=RaisingBus(),
            ),
            runs=RecordingRunRepository(),
            activations=type("Activations", (), {"ready_activations": lambda _self, _event: _one(artifact_ref_for(artifact))})(),
        ),
        adapter,
    )


async def _one(ref):
    return [ref]


def test_every_shadow_executor_declares_no_side_effects() -> None:
    assert all(executor.no_side_effects for executor in EXECUTORS[ExecutionMode.SHADOW].values())


@pytest.mark.asyncio
async def test_shadow_never_invokes_command_or_preview_or_bus() -> None:
    engine, adapter = build()

    result = await engine.dispatch_event(
        event("task-completed-code"), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW
    )

    assert result.rules_selected == ("review-on-task-completed",)
    assert adapter.calls == []
    assert adapter.preview_calls == []


class RaisingDependency:
    def __getattr__(self, name: str):
        raise AssertionError(f"shadow must not touch {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_step", ["ensure-review-task", "classify-risk", "escalate", "open-gate"])
async def test_shadow_never_touches_command_llm_task_gate_preview_or_bus(entry_step: str) -> None:
    engine, adapter = build()
    artifact = load_artifact("review-pipeline.artifact.json")
    rule = artifact.rules[0].model_copy(update={"entry_step": entry_step})
    artifact = artifact.model_copy(update={"rules": [rule, *artifact.rules[1:]]})
    engine.services.artifact_store.put(artifact)
    engine.services = EngineServices(
        contracts=engine.services.contracts,
        clock=engine.services.clock,
        artifact_store=engine.services.artifact_store,
        llm=RaisingDependency(),
        handler=RaisingDependency(),
        bus=RaisingBus(),
    )
    engine.activations = type(
        "Activations", (), {"ready_activations": lambda _self, _event: _one(artifact_ref_for(artifact))}
    )()

    result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW)

    assert result.rules_selected == ("review-on-task-completed",)
    assert adapter.calls == []
    assert adapter.preview_calls == []


@pytest.mark.asyncio
async def test_shadow_records_ordered_canonical_command_args() -> None:
    engine, _adapter = build()

    result = await engine.dispatch_event(
        event("task-completed-code"), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW
    )

    assert result.commands == (
        (
            "ensure-review-task",
            "ensure_task",
            '{"dedup_key":"review-of-task-1","project_id":"proj-1","title":"Review: Add the widget"}',
        ),
    )


@pytest.mark.asyncio
async def test_shadow_and_live_select_the_same_rules() -> None:
    shadow, _ = build()
    live, adapter = build()
    from src.commands.contracts.models import CommandResult
    from tests.fixtures.contracts.engine_contracts import EnsureTaskResult

    adapter.queue.append(
        CommandResult(outcome="created", value=EnsureTaskResult(task_id="t", created=True), summary="")
    )
    shadow_result = await shadow.dispatch_event(
        event("task-completed-code"), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW
    )
    live_result = await live.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)

    assert shadow_result.rules_selected == live_result.rules_selected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_name",
    [
        "task-completed-code",
        "task-completed-docs",
        "task-completed-no-project",
        "spec-approved",
        "spec-approved-empty-downstream",
        "task-created",
    ],
)
async def test_shadow_matches_live_rule_selection_for_fixture_corpus(event_name: str) -> None:
    shadow, _ = build()
    live, _ = build()

    shadow_result = await shadow.dispatch_event(event(event_name), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW)
    live_result = await live.dispatch_event(event(event_name), TRUSTED_LOCAL)

    assert shadow_result.rules_selected == live_result.rules_selected
