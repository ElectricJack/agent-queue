"""Bounded V2 dry-run traversal — Package 4 C5 / T-11."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL
from src.llm.types import TokenUsage
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.definition import ToolUsePolicy
from src.profiles.capabilities import CapabilityPolicy
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
    with_step,
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


class RaisingWrites:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dry-run touched durable state through {name}")


class ProfileStore:
    async def get_profile(self, _profile_id: str) -> Any:
        return SimpleNamespace(
            id="reviewer",
            allowed_tools=[],
            harness_tools=[],
            aq_commands=["ensure_task"],
            plugin_tools=[],
        )


class BuiltinResolver:
    def is_builtin(self, _name: str) -> bool:
        return True

    def is_plugin(self, _name: str) -> bool:
        return False

    def plugin_command_names(self) -> frozenset[str]:
        return frozenset()


class DryRunAi:
    def __init__(self) -> None:
        self.complete_calls = 0

    def resolve(self, _spec: Any) -> Any:
        return SimpleNamespace(model="dry-run-model")

    def _provider_for(self, _resolved: Any) -> Any:
        return SimpleNamespace(reports_usage=True)

    async def run_tools(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invoke_ai dry-run must not enter the live tool loop")

    async def complete(self, *_args: Any, **_kwargs: Any) -> Any:
        self.complete_calls += 1
        return SimpleNamespace(text='{"risk":"low"}', usage=TokenUsage(3, 1, True))


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


@pytest.mark.asyncio
async def test_invoke_ai_does_not_expose_or_dispatch_llm_tools() -> None:
    engine, adapter, ref = build()
    artifact = engine.services.artifact_store.load(ref.artifact_sha256)
    llm = artifact.steps["classify-risk"].model_copy(
        update={"tool_use": ToolUsePolicy(enabled=True, aq_commands=["ensure_task"])}
    )
    artifact = with_step(artifact, "classify-risk", llm)
    rule = artifact.rules[0].model_copy(update={"entry_step": "classify-risk"})
    artifact = artifact.model_copy(update={"rules": [rule, *artifact.rules[1:]]})
    engine.services.artifact_store.put(artifact)
    ai = DryRunAi()
    engine.services = replace(
        engine.services, llm=ai, db=ProfileStore(), resolver=BuiltinResolver(), authorization_mode="enforce"
    )

    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["ensure_task"]),
    )
    tree = await engine.dry_run(artifact_ref_for(artifact), event("task-completed-code"), principal, invoke_ai=True)

    assert ai.complete_calls > 0, tree
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_path_limit_is_hard_and_marks_every_path_not_completed() -> None:
    engine, _adapter, ref = build("two-rules-one-event.artifact.json")
    artifact = engine.services.artifact_store.load(ref.artifact_sha256)
    first = artifact.rules[0].model_copy(update={"entry_step": "review-done"})
    artifact = artifact.model_copy(update={"rules": [first, *artifact.rules[1:]]})
    engine.services.artifact_store.put(artifact)

    tree = await engine.dry_run(
        artifact_ref_for(artifact), event("task-completed-code"), TRUSTED_LOCAL, max_paths=1
    )

    assert tree.truncated is True
    assert len(tree.paths) == 1
    assert all(path.completed is False for path in tree.paths)


@pytest.mark.asyncio
async def test_visit_limit_never_reports_a_completed_path() -> None:
    adapter = ScriptedAdapter(
        preview=[CommandResult(outcome="listed", value=ListTasksResult(tasks=[], count=0), summary="")]
    )
    engine, _adapter, ref = build("sequential-loop.artifact.json", adapter=adapter)

    tree = await engine.dry_run(ref, event("spec-approved"), TRUSTED_LOCAL, max_step_visits=2)

    assert tree.truncated is True
    assert all(path.completed is False for path in tree.paths)


@pytest.mark.asyncio
async def test_dry_run_never_touches_run_or_wait_repositories() -> None:
    engine, _adapter, ref = build("wait-kinds.artifact.json")
    engine.runs = RaisingWrites()
    engine.waits = RaisingWrites()

    tree = await engine.dry_run(ref, event("task-completed-code"), TRUSTED_LOCAL)

    assert tree.paths[0].nodes[0].status == "unresolved"


@pytest.mark.asyncio
async def test_symbolic_result_references_remain_unresolved_downstream() -> None:
    engine, _adapter, ref = build()

    tree = await engine.dry_run(ref, event("task-completed-code"), TRUSTED_LOCAL)

    waits = [node for path in tree.paths for node in path.nodes if node.step_id == "await-approval"]
    assert waits
    assert all(node.status == "unresolved" for node in waits)
    assert all(path.completed is False for path in tree.paths)


@pytest.mark.asyncio
async def test_live_and_dry_run_select_identical_resolved_edges() -> None:
    artifact = load_artifact("sequential-loop.artifact.json")
    ref = artifact_ref_for(artifact)
    live_adapter = ScriptedAdapter(
        [CommandResult(outcome="listed", value=ListTasksResult(tasks=[], count=0), summary="")]
    )
    dry_adapter = ScriptedAdapter(
        preview=[CommandResult(outcome="listed", value=ListTasksResult(tasks=[], count=0), summary="")]
    )
    live, _live_adapter, _ = build("sequential-loop.artifact.json", adapter=live_adapter)
    dry, _dry_adapter, _ = build("sequential-loop.artifact.json", adapter=dry_adapter)

    live_outcome = await live.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
    dry_tree = await dry.dry_run(ref, event("spec-approved"), TRUSTED_LOCAL)

    live_edges = [
        (receipt.step_id, receipt.selected_transition.rsplit("::", 1)[-1], artifact.steps[receipt.step_id].transitions[receipt.selected_transition.rsplit("::", 1)[-1]])
        for receipt in live_outcome.receipts
        if receipt.selected_transition is not None
    ]
    dry_edges = [
        (node.step_id, node.outcome, node.target)
        for node in dry_tree.paths[0].nodes
        if node.target is not None
    ]
    assert dry_edges == live_edges
