"""Bounded V2 dry-run traversal — Package 4 C5 / T-11."""

from __future__ import annotations

from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import EngineServices, ExecutionMode
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    LIST_TASKS,
    ListTasksResult,
    ScriptedAdapter,
    registry_with,
)
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    artifact_ref_for,
    event,
    load_artifact,
)


def build(
    artifact_name: str = "review-pipeline.artifact.json", *, adapter: ScriptedAdapter | None = None
) -> tuple[PlaybookEngine, ScriptedAdapter, Any]:
    artifact = load_artifact(artifact_name)
    store = InMemoryArtifactStore()
    store.put(artifact)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS, adapter=adapter)
    engine = PlaybookEngine(
        services=EngineServices(contracts=registry, clock=lambda: 1_000.0, artifact_store=store),
        # A dry run must not reach this repository.  Keeping a real recording
        # double here catches accidental wiring without mocking traversal.
        runs=RecordingRunRepository(),
    )
    return engine, adapter, artifact_ref_for(artifact)


@pytest.mark.asyncio
async def test_deterministic_executors_are_identical_across_modes() -> None:
    for kind in ("decision", "foreach", "terminal"):
        live = executor_for(kind, ExecutionMode.LIVE)
        assert live is executor_for(kind, ExecutionMode.DRY_RUN)
        assert live is executor_for(kind, ExecutionMode.SHADOW)


@pytest.mark.asyncio
async def test_command_without_preview_is_unresolved_and_never_completes() -> None:
    engine, adapter, ref = build()

    tree = await engine.dry_run(ref, event("task-completed-code"), TRUSTED_LOCAL)

    assert adapter.calls == []
    assert tree.paths[0].nodes[0].status == "unresolved"
    assert tree.paths[0].nodes[0].reason == "no preview adapter"
    assert tree.paths[0].completed is False


@pytest.mark.asyncio
async def test_llm_forks_across_each_declared_outcome() -> None:
    engine, _adapter, ref = build()
    artifact = engine.services.artifact_store.load(ref.artifact_sha256)
    rule = artifact.rules[0].model_copy(update={"entry_step": "classify-risk"})
    artifact = artifact.model_copy(update={"rules": [rule, *artifact.rules[1:]]})
    engine.services.artifact_store.put(artifact)

    tree = await engine.dry_run(artifact_ref_for(artifact), event("task-completed-code"), TRUSTED_LOCAL)

    assert {path.nodes[0].outcome for path in tree.paths} == {
        "low",
        "high",
        "invalid_output",
        "budget_exceeded",
        "provider_error",
        "timed_out",
        "cancelled",
    }
    assert all(path.completed is False for path in tree.paths)


@pytest.mark.asyncio
async def test_resolved_foreach_expands_and_visit_limit_truncates() -> None:
    adapter = ScriptedAdapter(
        preview=[
            CommandResult(
                outcome="listed",
                value=ListTasksResult(tasks=[{"id": f"t-{n}"} for n in range(40)], count=40),
                summary="listed",
            )
        ]
    )
    engine, _adapter, ref = build("sequential-loop.artifact.json", adapter=adapter)

    tree = await engine.dry_run(
        ref,
        event("spec-approved"),
        TRUSTED_LOCAL,
        max_step_visits=2,
    )

    assert tree.truncated is True
    assert tree.paths[0].status == "truncated"
    assert tree.paths[0].nodes[-1].reason == "visit_limit"


@pytest.mark.asyncio
async def test_unresolved_foreach_collection_stops_at_the_loop_boundary() -> None:
    engine, _adapter, ref = build("sequential-loop.artifact.json")
    artifact = engine.services.artifact_store.load(ref.artifact_sha256)
    rule = artifact.rules[0].model_copy(update={"entry_step": "for-each-task"})
    artifact = artifact.model_copy(update={"rules": [rule]})
    engine.services.artifact_store.put(artifact)

    tree = await engine.dry_run(artifact_ref_for(artifact), event("spec-approved"), TRUSTED_LOCAL)

    assert tree.paths[0].nodes[0].status == "unresolved"
    assert tree.paths[0].completed is False


@pytest.mark.asyncio
async def test_invoke_ai_keeps_commands_preview_only() -> None:
    adapter = ScriptedAdapter(
        preview=[
            CommandResult(
                outcome="listed", value=ListTasksResult(tasks=[], count=0), summary="listed"
            )
        ]
    )
    engine, _adapter, ref = build("sequential-loop.artifact.json", adapter=adapter)

    await engine.dry_run(ref, event("spec-approved"), TRUSTED_LOCAL, invoke_ai=True)

    assert adapter.calls == []
    assert len(adapter.preview_calls) == 1
