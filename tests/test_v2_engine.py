"""The V2 playbook engine — Package 4 child plan T-1, T-3 and T-5.

Three V1 behaviours are the reason this suite exists, and each has a test
here that would fail against the live V1 runner:

* one run row covered every matching rule (``core.py:944-957`` forced every
  per-rule runner onto one ``run_id``), so a five-rule dispatch had the
  failure semantics "any rule fails the whole run";
* a business outcome with no edge ended the run as ``completed``
  (``pipeline_runner.py:151-158``);
* an unhandled step shape silently ended the walk (``runner.py:2270``).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import (
    DispatchResult,
    EventArrived,
    PlaybookEngine,
    RunOutcome,
)
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    ExecutorResult,
    StepControl,
    UnknownStepType,
)
from src.playbooks.receipts import RECEIPT_OUTCOMES, transition_id
from src.playbooks.run_state import RunLifecycle
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    LIST_TASKS,
    TWO_FAILURES,
    EnsureTaskResult,
    ListTasksResult,
    TwoFailuresResult,
    registry_with,
)
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
    event,
    load_artifact,
    with_step,
)


def ok(task_id: str = "t-1", created: bool = True) -> CommandResult:
    return CommandResult(
        outcome="created" if created else "reused",
        value=EnsureTaskResult(task_id=task_id, created=created),
        summary="ensured",
    )


def listed(count: int = 2) -> CommandResult:
    return CommandResult(
        outcome="listed",
        value=ListTasksResult(tasks=[{"id": "d-1"}], count=count),
        summary="listed",
    )


def build(
    artifact_name: str = "two-rules-one-event.artifact.json",
    *,
    contracts: tuple[Any, ...] = (ENSURE_TASK, LIST_TASKS),
    runs: RecordingRunRepository | None = None,
) -> tuple[PlaybookEngine, Any, RecordingRunRepository, RecordingBus, Any]:
    artifact = load_artifact(artifact_name)
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(*contracts)
    store = InMemoryArtifactStore()
    store.put(artifact)
    runs = runs or RecordingRunRepository()
    bus = RecordingBus()
    services = EngineServices(
        contracts=registry,
        clock=lambda: 1_000.0,
        artifact_store=store,
        bus=bus,
    )
    engine = PlaybookEngine(
        services=services,
        runs=runs,
        waits=None,
        activations=StubActivations([ref]),
    )
    return engine, adapter, runs, bus, ref


class TestRulePerRunDispatch:
    """§4.2 — the direct fix for one run row covering every matching rule."""

    @pytest.mark.asyncio
    async def test_two_matching_rules_produce_two_runs(self):
        engine, adapter, runs, _bus, _ref = build()
        adapter.queue.extend([ok(), listed()])
        result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        assert isinstance(result, DispatchResult)
        assert set(result.rules_selected) == {"review", "sweep"}
        assert len(result.run_ids) == 2
        assert len(set(result.run_ids)) == 2
        for run_id in result.run_ids:
            assert runs.snapshots[run_id].dispatch_id == result.dispatch_id

    @pytest.mark.asyncio
    async def test_a_rules_trigger_filter_can_reject(self):
        engine, adapter, _runs, _bus, _ref = build()
        adapter.queue.append(listed())
        result = await engine.dispatch_event(event("task-completed-docs"), TRUSTED_LOCAL)
        # ``review_task: True`` fails rule 1's filter; rule 2 has no filter.
        assert result.rules_selected == ("sweep",)

    @pytest.mark.asyncio
    async def test_sibling_failure_does_not_fail_the_other_run(self):
        engine, adapter, runs, _bus, _ref = build()
        adapter.queue.extend([RuntimeError("ensure_task exploded"), listed()])
        result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        lifecycles = {
            runs.snapshots[rid].rule_id: runs.snapshots[rid].lifecycle
            for rid in result.run_ids
        }
        assert lifecycles["review"] is RunLifecycle.FAILED
        assert lifecycles["sweep"] is RunLifecycle.COMPLETED

    @pytest.mark.asyncio
    async def test_same_event_id_dispatched_twice_creates_no_new_runs(self):
        engine, adapter, runs, _bus, _ref = build()
        adapter.queue.extend([ok(), listed()])
        first = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        before = len(runs.snapshots)
        second = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        assert second.run_ids == first.run_ids
        assert set(second.deduplicated) == {"review", "sweep"}
        assert len(runs.snapshots) == before

    @pytest.mark.asyncio
    async def test_the_dispatch_id_is_derived_from_the_event_id(self):
        """§2.5 item 9 — replay collides on the shipped (dispatch_id, rule_id)
        unique index rather than on a pre-read."""
        engine, adapter, _runs, _bus, _ref = build()
        adapter.queue.extend([ok(), listed()])
        first = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        engine2, adapter2, _r2, _b2, _ref2 = build()
        adapter2.queue.extend([ok(), listed()])
        second = await engine2.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        assert first.dispatch_id == second.dispatch_id

    @pytest.mark.asyncio
    async def test_an_event_without_an_id_is_not_deduplicated(self):
        engine, adapter, _runs, _bus, _ref = build()
        payload = dict(event("task-completed-code"))
        payload.pop("event_id")
        adapter.queue.extend([ok(), listed(), ok(), listed()])
        first = await engine.dispatch_event(payload, TRUSTED_LOCAL)
        second = await engine.dispatch_event(payload, TRUSTED_LOCAL)
        assert first.dispatch_id != second.dispatch_id
        assert set(first.run_ids).isdisjoint(second.run_ids)


class TestStepBoundary:
    @pytest.mark.asyncio
    async def test_a_run_walks_to_its_terminal(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert isinstance(outcome, RunOutcome)
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        assert runs.snapshots[outcome.run_id].current_step_id == "review-done"
        assert [r.step_id for r in runs.receipts] == ["ensure-review-task", "review-done"]

    @pytest.mark.asyncio
    async def test_business_outcome_without_an_edge_is_a_contract_violation(self):
        """The replacement for ``pipeline_runner.py:151-158``, where a missing
        edge ended the run as ``completed``."""
        engine, adapter, runs, _bus, _ref = build(
            "two-failure-outcomes.artifact.json", contracts=(TWO_FAILURES,)
        )
        artifact = load_artifact("two-failure-outcomes.artifact.json")
        # Pin an artifact whose step maps neither ``conflict`` nor
        # ``runtime_error`` — the shape a contract change leaves behind.
        narrowed = {
            name: target
            for name, target in artifact.steps["probe-step"].transitions.items()
            if name not in {"conflict", "runtime_error"}
        }
        stripped = with_step(
            artifact, "probe-step", artifact.steps["probe-step"].model_copy(
                update={"transitions": narrowed}
            )
        )
        engine.services.artifact_store.put(stripped)
        adapter.queue.append(
            CommandResult(outcome="conflict", value=TwoFailuresResult(), summary="")
        )
        outcome = await engine.run_rule(
            artifact_ref_for(stripped), "probe", event("task-completed-code"), TRUSTED_LOCAL
        )
        assert outcome.lifecycle is RunLifecycle.FAILED
        assert outcome.outcome == "contract_violation"
        assert runs.snapshots[outcome.run_id].error_code == "contract_violation"

    @pytest.mark.asyncio
    async def test_two_failure_outcomes_take_different_edges(self):
        engine, adapter, runs, _bus, ref = build(
            "two-failure-outcomes.artifact.json", contracts=(TWO_FAILURES,)
        )
        selected = []
        for name in ("not_found", "conflict"):
            adapter.queue.append(
                CommandResult(outcome=name, value=TwoFailuresResult(detail=name), summary="")
            )
            payload = dict(event("task-completed-code"))
            payload["event_id"] = f"evt-{name}"
            outcome = await engine.run_rule(ref, "probe", payload, TRUSTED_LOCAL)
            step = next(r for r in runs.receipts if r.run_id == outcome.run_id)
            selected.append(step.selected_transition)
        assert selected[0] != selected[1]
        assert selected[0] == transition_id("probe", "probe-step", "not_found")

    @pytest.mark.asyncio
    async def test_input_resolution_failure_never_reaches_the_executor(self):
        """§3.4 step 4 — the miss is an outcome, and the artifact routes it
        down its declared ``runtime_error`` edge rather than the engine
        coercing the reference to ``""``."""
        engine, adapter, runs, _bus, ref = build()
        payload = dict(event("task-completed-code"))
        payload.pop("project_id")
        outcome = await engine.run_rule(ref, "review", payload, TRUSTED_LOCAL)
        assert adapter.calls == []
        first = runs.receipts[0]
        assert first.error_code == "input_resolution_failed"
        assert first.selected_transition == transition_id(
            "review", "ensure-review-task", "input_resolution_failed"
        )
        assert runs.snapshots[outcome.run_id].current_step_id == "review-failed"
        assert outcome.lifecycle is RunLifecycle.FAILED

    @pytest.mark.asyncio
    async def test_unknown_step_type_raises_rather_than_terminating(self):
        _engine, _adapter, _runs, _bus, _ref = build()
        with pytest.raises(UnknownStepType):
            executor_for("frobnicate", ExecutionMode.LIVE)

    @pytest.mark.asyncio
    async def test_a_deadline_that_has_passed_times_the_run_out(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        outcome = await engine.run_rule(
            ref, "review", event("task-completed-code"), TRUSTED_LOCAL, deadline_at=1.0
        )
        assert outcome.lifecycle is RunLifecycle.TIMED_OUT
        assert outcome.outcome == "timed_out"
        assert adapter.calls == []
        assert runs.receipts[-1].timed_out is True


class TestCommitBoundary:
    """T-3 — exactly one durable write per attempt, and never a retry."""

    @pytest.mark.asyncio
    async def test_exactly_one_commit_per_attempt(self):
        engine, adapter, runs, _bus, _ref = build()
        adapter.queue.extend([ok(), listed()])
        await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        assert runs.commit_calls == len(runs.receipts)
        identities = [(r.run_id, r.step_id, r.iteration, r.attempt) for r in runs.receipts]
        assert len(identities) == len(set(identities))

    @pytest.mark.asyncio
    async def test_no_durable_write_happens_before_the_boundary(self):
        """The side effect is real; the snapshot did not move and no bus event
        was emitted.  Proves the §3.4 step-11 ordering."""
        runs = RecordingRunRepository(fail_commit_with=RuntimeError("db down"))
        engine, adapter, runs, bus, ref = build(runs=runs)
        adapter.queue.append(ok())
        with pytest.raises(RuntimeError):
            await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert adapter.names == ["ensure_task"]
        assert runs.receipts == []
        # ``run.started`` marks the run row, which did land; no *step* event
        # may precede the boundary that would have made the step durable.
        assert [name for name, _ in bus.events] == ["playbook.v2.run.started"]

    @pytest.mark.asyncio
    async def test_version_conflict_fails_the_run_and_receipts_it(self):
        """Two writers at one boundary means two engines think they own the
        run; silently merging them is how a side-effecting command runs
        twice, so the run fails instead."""
        runs = RecordingRunRepository(conflict_on_commit=1)
        engine, adapter, runs, _bus, ref = build(runs=runs)
        adapter.queue.append(ok())
        outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.FAILED
        assert outcome.outcome == "interrupted"
        assert runs.conflicts == 1
        assert any(r.error_code == "interrupted" for r in runs.receipts)

    @pytest.mark.asyncio
    async def test_the_step_completed_event_follows_the_commit(self):
        engine, adapter, runs, bus, ref = build()
        adapter.queue.append(ok())
        await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        kinds = [name for name, _ in bus.events]
        assert kinds.count("playbook.v2.step.completed") == len(runs.receipts)
        assert kinds[0] == "playbook.v2.run.started"
        assert kinds[-1] == "playbook.v2.run.finished"


class TestReceipts:
    @pytest.mark.asyncio
    async def test_every_receipt_pins_the_executed_artifact(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert {r.artifact_sha256 for r in runs.receipts} == {ref.artifact_sha256}

    @pytest.mark.asyncio
    async def test_every_receipt_carries_a_declared_classification(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        for receipt in runs.receipts:
            assert receipt.outcome in RECEIPT_OUTCOMES
            assert receipt.rule_id == "review"
            assert receipt.snapshot_version >= 0
            assert receipt.idempotency_key

    @pytest.mark.asyncio
    async def test_a_failure_outcome_is_classified_as_a_failure(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(
            CommandResult(
                outcome="rejected",
                value=EnsureTaskResult(task_id="", created=False),
                summary="no",
            )
        )
        await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        first = runs.receipts[0]
        assert first.outcome == "failure"
        assert first.selected_transition == transition_id(
            "review", "ensure-review-task", "rejected"
        )

    @pytest.mark.asyncio
    async def test_the_receipt_never_carries_an_unprojected_field(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        projected = set(ENSURE_TASK.execution.receipt_projection)
        assert set(runs.receipts[0].result) <= projected


class TestDecisionAndTerminal:
    """T-5."""

    @pytest.mark.asyncio
    async def test_decision_takes_the_first_true_case(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(listed(count=2))
        outcome = await engine.run_rule(ref, "sweep", event("task-completed-code"), TRUSTED_LOCAL)
        assert runs.snapshots[outcome.run_id].current_step_id == "sweep-done"

    @pytest.mark.asyncio
    async def test_decision_falls_through_to_default(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(listed(count=0))
        outcome = await engine.run_rule(ref, "sweep", event("task-completed-code"), TRUSTED_LOCAL)
        assert runs.snapshots[outcome.run_id].current_step_id == "sweep-empty"

    @pytest.mark.asyncio
    async def test_decision_makes_no_llm_call(self):
        class Exploding:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"a decision reached services.llm.{name}")

        engine, adapter, _runs, _bus, ref = build()
        object.__setattr__(engine.services, "llm", Exploding())
        adapter.queue.append(listed(count=1))
        outcome = await engine.run_rule(ref, "sweep", event("task-completed-code"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.COMPLETED

    @pytest.mark.asyncio
    async def test_decision_goto_outside_declared_targets_is_a_contract_violation(self):
        engine, _adapter, _runs, _bus, _ref = build()
        artifact = load_artifact("two-rules-one-event.artifact.json")
        step = artifact.steps["check-empty"]
        forged = ExecutorResult(
            control=StepControl.GOTO, outcome="matched", goto_step_id="review-done"
        )
        violation = engine.validate_control(step, forged)
        assert violation == "contract_violation"

    @pytest.mark.asyncio
    async def test_command_executor_cannot_goto(self):
        """``GOTO`` is exposed only by the typed contracts that declare a
        runtime-chosen target — decision and foreach."""
        engine, _adapter, _runs, _bus, _ref = build()
        artifact = load_artifact("two-rules-one-event.artifact.json")
        step = artifact.steps["ensure-review-task"]
        forged = ExecutorResult(
            control=StepControl.GOTO, outcome="created", goto_step_id="review-done"
        )
        assert engine.validate_control(step, forged) == "contract_violation"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("terminal", "lifecycle"),
        [
            ("completed", RunLifecycle.COMPLETED),
            ("failed", RunLifecycle.FAILED),
            ("cancelled", RunLifecycle.CANCELLED),
        ],
    )
    async def test_terminal_outcome_maps_onto_the_run_lifecycle(self, terminal, lifecycle):
        engine, adapter, _runs, _bus, _ref = build()
        artifact = load_artifact("two-rules-one-event.artifact.json")
        mutated = with_step(
            artifact,
            "review-done",
            artifact.steps["review-done"].model_copy(update={"outcome": terminal}),
        )
        engine.services.artifact_store.put(mutated)
        adapter.queue.append(ok())
        outcome = await engine.run_rule(
            artifact_ref_for(mutated), "review", event("task-completed-code"), TRUSTED_LOCAL
        )
        assert outcome.lifecycle is lifecycle


class TestModes:
    @pytest.mark.asyncio
    async def test_deterministic_executors_are_identical_across_modes(self):
        for kind in ("decision", "terminal"):
            live = executor_for(kind, ExecutionMode.LIVE)
            assert live is executor_for(kind, ExecutionMode.DRY_RUN)
            assert live is executor_for(kind, ExecutionMode.SHADOW)

    @pytest.mark.asyncio
    async def test_shadow_mode_never_reaches_the_run_repository(self):
        """§3.3.5's structural half: the real repository is not wired in at
        all for a non-live mode."""
        engine, adapter, runs, _bus, ref = build()
        outcome = await engine.run_rule(
            ref,
            "review",
            event("task-completed-code"),
            TRUSTED_LOCAL,
            mode=ExecutionMode.SHADOW,
        )
        assert runs.commit_calls == 0
        assert runs.snapshots == {}
        assert adapter.calls == []
        assert outcome.receipts


class TestResume:
    @pytest.mark.asyncio
    async def test_resuming_a_terminal_run_is_refused(self):
        engine, adapter, _runs, _bus, ref = build()
        adapter.queue.append(ok())
        outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        again = await engine.resume(
            outcome.run_id, EventArrived(event_id="x", payload={}), TRUSTED_LOCAL
        )
        assert again.outcome == "already_terminal"
        assert again.lifecycle is RunLifecycle.COMPLETED

    @pytest.mark.asyncio
    async def test_resuming_an_unknown_run_is_refused(self):
        engine, _adapter, _runs, _bus, _ref = build()
        outcome = await engine.resume(
            "no-such-run", EventArrived(event_id="x", payload={}), TRUSTED_LOCAL
        )
        assert outcome.outcome == "unknown_run"

    @pytest.mark.asyncio
    async def test_a_paused_run_continues_from_its_current_step(self):
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        outcome = await engine.run_rule(
            ref, "review", event("task-completed-code"), TRUSTED_LOCAL, pause_before_start=True
        )
        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert runs.receipts == []
        adapter.queue.append(ok())
        resumed = await engine.resume(
            outcome.run_id, EventArrived(event_id="x", payload={}), TRUSTED_LOCAL
        )
        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert [r.step_id for r in runs.receipts] == ["ensure-review-task", "review-done"]


def test_no_executor_module_imports_the_engine():
    """§3.4's "the executor cannot skip a boundary", enforced structurally.

    An executor that wants to continue *returns*; there is no path from one
    back into the engine, so an executor cannot skip a durable boundary even
    by mistake.
    """
    import ast
    import pathlib

    for path in pathlib.Path("src/playbooks/executors").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "src.playbooks.engine", path
            elif isinstance(node, ast.Import):
                assert all(a.name != "src.playbooks.engine" for a in node.names), path
