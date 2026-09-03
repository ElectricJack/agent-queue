"""Side-effect-free V2 shadow execution — Package 4 C5 / T-12."""

from __future__ import annotations

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS
from src.playbooks.executors.base import EngineServices, ExecutionMode
from tests.fixtures.contracts.engine_contracts import ENSURE_TASK, LIST_TASKS, ScriptedAdapter, registry_with
from tests.playbook_v2_engine_helpers import InMemoryArtifactStore, RecordingRunRepository, artifact_ref_for, event, load_artifact


class RaisingBus:
    async def emit(self, *_args, **_kwargs) -> None:
        raise AssertionError("shadow must not emit bus events")


def build() -> tuple[PlaybookEngine, ScriptedAdapter]:
    artifact = load_artifact("review-pipeline.artifact.json")
    store = InMemoryArtifactStore()
    store.put(artifact)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS)
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
