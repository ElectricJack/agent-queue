"""Side-effect-free V2 shadow execution — Package 4 C5 / T-12."""

from __future__ import annotations

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors import EXECUTORS
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.run_state import RunLifecycle
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    GATE_CREATE,
    LIST_TASKS,
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
            activations=type("Activations", (), {"ready_activations": lambda _self, _event, _payload=None: _one(artifact_ref_for(artifact))})(),
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
        "Activations", (), {"ready_activations": lambda _self, _event, _payload=None: _one(artifact_ref_for(artifact))}
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


# --------------------------------------------------------------------------
# Shadow traverses beyond an unresolved effect (wise-apex-40)
# --------------------------------------------------------------------------
#
# ``run_rule(mode=SHADOW)`` used to drive the live cursor, so the first
# ``UNRESOLVED`` command paused the run at that very step and no downstream
# decision was ever compared.  Shadow now walks the same bounded symbolic
# work list dry-run does, with the shadow executor table.


def _nodes(tree):
    return [node for path in tree.paths for node in path.nodes]


@pytest.mark.asyncio
async def test_shadow_run_traverses_past_the_first_unresolved_command() -> None:
    engine, adapter = build()
    artifact = load_artifact("review-pipeline.artifact.json")

    outcome = await engine.run_rule(
        artifact_ref_for(artifact),
        "review-on-task-completed",
        event("task-completed-code"),
        TRUSTED_LOCAL,
        mode=ExecutionMode.SHADOW,
    )

    assert outcome.lifecycle is RunLifecycle.COMPLETED
    assert outcome.outcome in {"unresolved", "truncated"}
    assert outcome.snapshot is not None and outcome.snapshot.mode == "shadow"
    assert outcome.traversal is not None
    visited = {node.step_id for node in _nodes(outcome.traversal)}
    assert {
        "ensure-review-task",
        "classify-risk",
        "escalate",
        "await-approval",
        "done",
        "review-unavailable",
    } <= visited
    assert adapter.calls == [] and adapter.preview_calls == []


@pytest.mark.asyncio
async def test_shadow_command_node_reports_its_possible_outcomes_and_targets() -> None:
    engine, _adapter = build()
    artifact = load_artifact("review-pipeline.artifact.json")

    outcome = await engine.run_rule(
        artifact_ref_for(artifact),
        "review-on-task-completed",
        event("task-completed-code"),
        TRUSTED_LOCAL,
        mode=ExecutionMode.SHADOW,
    )

    first = [node for node in _nodes(outcome.traversal) if node.step_id == "ensure-review-task"]
    assert first
    assert all(node.status == "unresolved" for node in first)
    assert all(node.possible_outcomes == ("created", "rejected", "reused") for node in first)
    assert {(node.outcome, node.target) for node in first} >= {
        ("created", "classify-risk"),
        ("reused", "classify-risk"),
        ("rejected", "review-unavailable"),
    }
    # A forked path never pretends it finished.
    assert all(path.completed is False for path in outcome.traversal.paths)


@pytest.mark.asyncio
async def test_shadow_records_each_intended_command_once_across_forks() -> None:
    engine, _adapter = build()

    result = await engine.dispatch_event(
        event("task-completed-code"), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW
    )

    # ``await-approval: revise`` routes back to ``ensure-review-task``, so the
    # bounded traversal visits it again with identical arguments.  Parity
    # compares intended calls, and the live run would issue that call once
    # per pass; the record is deduplicated by its canonical triple so a fork
    # count does not multiply it.
    assert len(result.commands) == 1
    assert result.commands[0][:2] == ("ensure-review-task", "ensure_task")
    assert len(result.traversals) == 1
    assert result.traversals[0].rules_selected == ("review-on-task-completed",)


@pytest.mark.asyncio
async def test_shadow_traversal_is_bounded() -> None:
    engine, _adapter = build()
    engine.max_symbolic_paths = 4
    artifact = load_artifact("review-pipeline.artifact.json")

    outcome = await engine.run_rule(
        artifact_ref_for(artifact),
        "review-on-task-completed",
        event("task-completed-code"),
        TRUSTED_LOCAL,
        mode=ExecutionMode.SHADOW,
    )

    assert outcome.outcome == "truncated"
    assert outcome.traversal.truncated is True
    assert len(outcome.traversal.paths) <= 4
    assert all(path.completed is False for path in outcome.traversal.paths)


@pytest.mark.asyncio
async def test_shadow_and_dry_run_take_the_same_edges_for_the_same_unresolved_boundaries() -> None:
    """Mode selects executors, not a graph: with no preview adapter the two
    symbolic modes must fork identically."""
    shadow, _ = build()
    dry, _ = build()
    artifact = load_artifact("review-pipeline.artifact.json")
    ref = artifact_ref_for(artifact)

    shadow_outcome = await shadow.run_rule(
        ref, "review-on-task-completed", event("task-completed-code"), TRUSTED_LOCAL,
        mode=ExecutionMode.SHADOW,
    )
    dry_tree = await dry.dry_run(ref, event("task-completed-code"), TRUSTED_LOCAL)

    def edges(tree):
        return [
            tuple((node.step_id, node.outcome, node.target) for node in path.nodes)
            for path in tree.paths
        ]

    assert edges(shadow_outcome.traversal) == edges(dry_tree)


@pytest.mark.asyncio
async def test_shadow_routes_an_authorization_denial_like_live() -> None:
    """Package 6 compares authorization decisions too, so shadow asks the
    same resolver live asks and takes the same edge."""
    from src.commands.principal import ExecutionPrincipal, PrincipalKind
    from src.playbooks.executors.base import EngineServices as _Services
    from src.profiles.capabilities import CapabilityPolicy

    engine, adapter = build()
    denied = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["list_tasks"]),
    )

    class DenyAll:
        def is_builtin(self, _name: str) -> bool:
            return True

        def is_plugin(self, _name: str) -> bool:
            return False

        def plugin_command_names(self) -> frozenset[str]:
            return frozenset()

    engine.services = _Services(
        contracts=engine.services.contracts,
        clock=engine.services.clock,
        artifact_store=engine.services.artifact_store,
        bus=RaisingBus(),
        resolver=DenyAll(),
        authorization_mode="enforce",
    )
    artifact = load_artifact("review-pipeline.artifact.json")

    outcome = await engine.run_rule(
        artifact_ref_for(artifact),
        "review-on-task-completed",
        event("task-completed-code"),
        denied,
        mode=ExecutionMode.SHADOW,
    )

    first = [node for node in _nodes(outcome.traversal) if node.step_id == "ensure-review-task"]
    assert first
    # ``unauthorized`` is reserved, so it takes the artifact's ``runtime_error``
    # edge exactly as ``_advance_on_outcome`` would live.
    assert all(
        (node.status, node.outcome, node.target)
        == ("resolved", "unauthorized", "review-unavailable")
        for node in first
    )
    assert outcome.commands == ()
    assert adapter.calls == [] and adapter.preview_calls == []
