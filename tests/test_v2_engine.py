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

from dataclasses import replace
from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import (
    DispatchResult,
    EventArrived,
    HumanDecision,
    OperatorResolution,
    PlaybookEngine,
    RunOutcome,
    TimerFired,
    WaitScheduler,
)
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
    UnknownStepType,
)
from src.playbooks.executors.foreach import ITERATING_OUTCOME
from src.playbooks.executors.wait import UNRESOLVED_REASON
from src.playbooks.expressions import BindingRef, ResolutionScope
from src.playbooks.receipts import RECEIPT_OUTCOMES, transition_id
from src.playbooks.run_state import LoopFrame, RunLifecycle
from src.playbooks.waits import (
    EMPTY_WAIT_CHANGES,
    WaitClaim,
    WaitRegistration,
    WaitSpec,
)
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


# --------------------------------------------------------------------------
# T-6 — durable waits (child plan §4.6)
#
# The child plan's §5.2 names these tests in ``tests/test_v2_waits.py``; the
# roadmap's Package 4 file list — which the task carrying this work names as
# the scope authority — creates only ``test_v2_engine.py``, so the test names
# are kept verbatim and live here.  §2.5 item 12 records the reconciliation.
# --------------------------------------------------------------------------


def wait_build(
    artifact_name: str = "wait-kinds.artifact.json",
    *,
    contracts: tuple[Any, ...] = (ENSURE_TASK, LIST_TASKS),
    runs: RecordingRunRepository | None = None,
    waits: Any | None = None,
    clock: Any | None = None,
) -> tuple[PlaybookEngine, Any, RecordingRunRepository, Any, Any]:
    artifact = load_artifact(artifact_name)
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(*contracts)
    store = InMemoryArtifactStore()
    store.put(artifact)
    runs = runs or RecordingRunRepository()
    services = EngineServices(
        contracts=registry,
        clock=clock or (lambda: 1_000.0),
        artifact_store=store,
        bus=RecordingBus(),
    )
    engine = PlaybookEngine(
        services=services,
        runs=runs,
        waits=waits,
        activations=StubActivations([ref]),
    )
    return engine, adapter, runs, waits, ref


class RecordingWaitRepository:
    """A ``WaitRepository`` double that records registration and expiry.

    ``matched_immediately`` is the whole point of the interface, so the
    double can be told to return one — which is how the "the event arrived
    before the pause was persisted" ordering is expressed without a database.
    """

    def __init__(self, *, inbox: dict[str, dict[str, Any]] | None = None) -> None:
        self.registered: list[WaitSpec] = []
        self.cleared: list[str] = []
        self.active: dict[str, WaitSpec] = {}
        self.inbox = dict(inbox or {})
        self.due: list[WaitClaim] = []

    async def register(self, wait, snapshot_version, *, conn=None):
        self.registered.append(wait)
        self.active[wait.wait_id] = wait
        payload = self.inbox.pop(wait.event_type, None) if wait.kind == "event" else None
        matched = None
        if payload is not None:
            matched = WaitClaim(
                wait_id=wait.wait_id,
                run_id=wait.run_id,
                step_id=wait.step_id,
                iteration=wait.iteration,
                kind=wait.kind,
                snapshot_version=snapshot_version,
                claimed_event_id="inbox-1",
                claimed_at=1_000.0,
                event_type=wait.event_type,
                event_fields=dict(payload),
            )
        return WaitRegistration(wait_id=wait.wait_id, matched_immediately=matched)

    async def clear_for_run(self, run_id, *, conn=None):
        return 0

    async def expire_due(self, now, *, limit=100):
        claims, self.due = self.due, []
        return claims

    async def list_active(self, run_id):
        return [w for w in self.active.values() if w.run_id == run_id]


class WaitAwareRepository(RecordingRunRepository):
    """``RecordingRunRepository`` plus Package 3's wait-change application.

    ``commit_boundary`` is the *only* place a wait is registered or cleared,
    so the double has to apply the change set for the engine's ordering to be
    observable at all — including handing an immediate inbox match back on
    the returned snapshot, which is what stops the run from sleeping on an
    event that has already been delivered.
    """

    def __init__(self, waits: RecordingWaitRepository, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.waits = waits
        self.wait_changes: list[Any] = []

    async def commit_boundary(self, snapshot, receipt, wait_changes=EMPTY_WAIT_CHANGES):
        self.wait_changes.append(wait_changes)
        stored = await super().commit_boundary(snapshot, receipt, wait_changes)
        if wait_changes.clear_wait_ids:
            self.waits.cleared.extend(wait_changes.clear_wait_ids)
            for wait_id in wait_changes.clear_wait_ids:
                self.waits.active.pop(wait_id, None)
        claims: list[WaitClaim] = []
        for wait in wait_changes.register:
            registration = await self.waits.register(wait, stored.version)
            if registration.matched_immediately is not None:
                claims.append(registration.matched_immediately)
        if claims:
            stored = replace(
                stored, pending_wait_claims=stored.pending_wait_claims + tuple(claims)
            )
            self.snapshots[stored.run_id] = stored
        return stored


def wait_engine(
    *, inbox: dict[str, dict[str, Any]] | None = None, clock: Any | None = None
) -> tuple[PlaybookEngine, WaitAwareRepository, RecordingWaitRepository, Any]:
    waits = RecordingWaitRepository(inbox=inbox)
    runs = WaitAwareRepository(waits)
    engine, _adapter, _runs, _w, ref = wait_build(runs=runs, waits=waits, clock=clock)
    return engine, runs, waits, ref


class TestDurableWaits:
    @pytest.mark.asyncio
    async def test_reaching_a_wait_pauses_the_run_and_registers_once(self):
        engine, runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert len(waits.registered) == 1
        spec = waits.registered[0]
        assert spec.kind == "human"
        # The exact typed correlation key, computed at pause from this run's
        # own scope — not a template kept for later rendering.
        assert spec.match == {"task_id": "task-1"}
        assert spec.deadline_at == 1_000.0 + 86_400
        # One boundary: the paused snapshot, the receipt and the registration.
        assert runs.commit_calls == 1
        assert runs.snapshots[outcome.run_id].wait is not None

    @pytest.mark.asyncio
    async def test_the_suspension_and_the_registration_are_one_boundary(self):
        engine, runs, waits, ref = wait_engine()
        await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
        assert len(runs.wait_changes) == 1
        assert runs.wait_changes[0].register == (waits.registered[0],)
        assert runs.receipts[0].wait_id == waits.registered[0].wait_id
        # Nothing was decided, so the receipt classifies as neither a success
        # nor a failure — the resume boundary carries the real outcome.
        assert runs.receipts[0].outcome == "skipped"
        assert runs.receipts[0].selected_transition is None

    @pytest.mark.asyncio
    async def test_event_before_registration_resumes_immediately(self):
        """The inbox already holds the match when the wait is registered."""
        engine, runs, _waits, ref = wait_engine(
            inbox={"review.finished": {"review_id": "r-1"}}
        )
        outcome = await engine.run_rule(ref, "correlate", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        # It never sleeps: the pause and the resume are two boundaries of one
        # walk, and there is exactly one resume receipt.
        resumes = [r for r in runs.receipts if r.step_id == "await-review" and r.attempt == 2]
        assert len(resumes) == 1
        assert resumes[0].selected_transition == transition_id(
            "correlate", "await-review", "matched"
        )
        assert runs.snapshots[outcome.run_id].bindings["review"] == {
            "event_type": "review.finished",
            "payload": {"review_id": "r-1"},
        }

    @pytest.mark.asyncio
    async def test_event_after_registration_resumes_exactly_once(self):
        engine, runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "correlate", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.PAUSED
        resumed = await engine.resume(
            outcome.run_id,
            EventArrived(event_id="evt-review", payload={"review_id": "r-2"}),
            TRUSTED_LOCAL,
        )
        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert len([r for r in runs.receipts if r.step_id == "await-review"]) == 2
        # The wait is deregistered in the same boundary that advances the run,
        # so there is no window in which one suspension is claimable twice.
        assert waits.cleared == [waits.registered[0].wait_id]
        assert runs.snapshots[outcome.run_id].wait is None

    @pytest.mark.asyncio
    async def test_a_second_delivery_after_the_resume_changes_nothing(self):
        engine, runs, _waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "correlate", event("spec-approved"), TRUSTED_LOCAL)
        await engine.resume(
            outcome.run_id, EventArrived(event_id="e", payload={}), TRUSTED_LOCAL
        )
        before = len(runs.receipts)
        again = await engine.resume(
            outcome.run_id, EventArrived(event_id="e", payload={}), TRUSTED_LOCAL
        )
        assert again.outcome == "already_terminal"
        assert len(runs.receipts) == before

    @pytest.mark.asyncio
    async def test_a_human_gate_routes_on_its_declared_outcome(self):
        engine, runs, _waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
        resumed = await engine.resume(
            outcome.run_id,
            HumanDecision(decision="revise", payload={"resolved_by": "jack"}),
            TRUSTED_LOCAL,
        )
        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert runs.snapshots[outcome.run_id].current_step_id == "gate-revised"
        assert runs.snapshots[outcome.run_id].bindings["approval"]["resolution"] == "revise"

    @pytest.mark.asyncio
    async def test_a_human_answer_outside_the_gate_vocabulary_is_a_contract_violation(self):
        """An answer the artifact never declared cannot invent an edge."""
        engine, runs, _waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
        resumed = await engine.resume(
            outcome.run_id, HumanDecision(decision="ship-it"), TRUSTED_LOCAL
        )
        assert resumed.snapshot.current_step_id == "gate-failed"
        assert "approval" not in resumed.snapshot.bindings
        assert [r.error_code for r in runs.receipts if r.step_id == "await-approval"][-1] == (
            "contract_violation"
        )

    @pytest.mark.asyncio
    async def test_a_timer_wait_fires_rather_than_timing_out(self):
        """A timer's deadline *is* its success; every other kind's is not."""
        engine, runs, _waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "sleep", event("task-created"), TRUSTED_LOCAL)
        resumed = await engine.resume(outcome.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert runs.snapshots[outcome.run_id].current_step_id == "sleep-done"
        assert "fired_at" in runs.snapshots[outcome.run_id].bindings["timer"]

    @pytest.mark.asyncio
    async def test_an_expired_event_wait_takes_the_timed_out_edge(self):
        engine, runs, _waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "correlate", event("spec-approved"), TRUSTED_LOCAL)
        resumed = await engine.resume(outcome.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
        assert resumed.snapshot.current_step_id == "correlate-failed"
        receipt = [r for r in runs.receipts if r.step_id == "await-review"][-1]
        assert receipt.timed_out is True
        assert receipt.outcome == "timeout"
        assert receipt.error == "wait deadline fired"

    @pytest.mark.parametrize(
        ("run_deadline", "expected_deadline", "expected_error"),
        [
            (1_000.0 + 60, 1_060.0, "run deadline fired"),
            (1_000.0 + 9_000, 1_600.0, "wait deadline fired"),
        ],
    )
    @pytest.mark.asyncio
    async def test_wait_deadline_and_run_deadline_the_earlier_wins(
        self, run_deadline, expected_deadline, expected_error
    ):
        engine, runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(
            ref,
            "correlate",
            event("spec-approved"),
            TRUSTED_LOCAL,
            deadline_at=run_deadline,
        )
        assert waits.registered[0].deadline_at == expected_deadline
        await engine.resume(outcome.run_id, TimerFired(wait_id=""), TRUSTED_LOCAL)
        receipt = [r for r in runs.receipts if r.step_id == "await-review"][-1]
        assert receipt.error == expected_error

    @pytest.mark.asyncio
    async def test_wait_does_not_create_a_timer_service_entry(self):
        """§4.6: ``TimerService`` schedules playbook *triggers*.

        A per-run wait is neither cron-like nor operator-visible, so the
        scheduler owns ``deadline_at`` and the trigger scheduler is untouched.
        """

        class ExplodingTimerService:
            def __getattr__(self, name):
                raise AssertionError(f"the wait path reached TimerService.{name}")

        engine, _runs, waits, ref = wait_engine()
        engine.services = replace(engine.services, db=ExplodingTimerService())
        await engine.run_rule(ref, "correlate", event("spec-approved"), TRUSTED_LOCAL)
        assert waits.registered[0].deadline_at == 1_600.0

    @pytest.mark.asyncio
    async def test_a_resume_with_nothing_to_resume_on_leaves_the_run_paused(self):
        engine, runs, _waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "correlate", event("spec-approved"), TRUSTED_LOCAL)
        commits = runs.commit_calls
        resumed = await engine.resume(
            outcome.run_id, OperatorResolution(kind="unknown"), TRUSTED_LOCAL
        )
        assert resumed.lifecycle is RunLifecycle.PAUSED
        assert runs.commit_calls == commits

    @pytest.mark.asyncio
    async def test_an_unresolvable_correlation_key_never_registers_a_wait(self):
        engine, _runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(
            ref, "correlate", {"event_type": "spec.approved"}, TRUSTED_LOCAL
        )
        assert waits.registered == []
        assert outcome.snapshot.current_step_id == "correlate-failed"

    @pytest.mark.asyncio
    async def test_the_reporting_wait_never_registers_and_never_guesses(self):
        engine, runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(
            ref,
            "correlate",
            event("spec-approved"),
            TRUSTED_LOCAL,
            mode=ExecutionMode.SHADOW,
        )
        assert waits.registered == []
        assert runs.commit_calls == 0
        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert outcome.receipts[-1].error == UNRESOLVED_REASON

    @pytest.mark.asyncio
    async def test_the_reporting_wait_is_shared_by_dry_run_and_shadow(self):
        preview = executor_for("wait", ExecutionMode.DRY_RUN)
        assert preview is executor_for("wait", ExecutionMode.SHADOW)
        assert preview.no_side_effects is True
        assert executor_for("wait", ExecutionMode.LIVE) is not preview


class TestWaitScheduler:
    @pytest.mark.asyncio
    async def test_the_scheduler_resumes_every_due_wait(self):
        engine, runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "sleep", event("task-created"), TRUSTED_LOCAL)
        waits.due = [
            WaitClaim(
                wait_id=waits.registered[0].wait_id,
                run_id=outcome.run_id,
                step_id="await-timer",
                iteration=-1,
                kind="timer",
                snapshot_version=1,
                claimed_event_id=None,
                claimed_at=1_030.0,
                expired=True,
            )
        ]
        scheduler = WaitScheduler(engine, waits, TRUSTED_LOCAL)
        assert await scheduler.tick(1_031.0) == (outcome.run_id,)
        assert runs.snapshots[outcome.run_id].lifecycle is RunLifecycle.COMPLETED

    @pytest.mark.asyncio
    async def test_a_scheduler_tick_with_nothing_due_writes_nothing(self):
        engine, runs, waits, ref = wait_engine()
        await engine.run_rule(ref, "sleep", event("task-created"), TRUSTED_LOCAL)
        commits = runs.commit_calls
        assert await WaitScheduler(engine, waits, TRUSTED_LOCAL).tick(1_001.0) == ()
        assert runs.commit_calls == commits

    @pytest.mark.asyncio
    async def test_the_sweep_continues_past_a_run_it_cannot_resume(self):
        engine, _runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "sleep", event("task-created"), TRUSTED_LOCAL)
        waits.due = [
            WaitClaim(
                wait_id="w-missing",
                run_id="no-such-run",
                step_id="await-timer",
                iteration=-1,
                kind="timer",
                snapshot_version=1,
                claimed_event_id=None,
                claimed_at=1_030.0,
                expired=True,
            ),
            WaitClaim(
                wait_id=waits.registered[0].wait_id,
                run_id=outcome.run_id,
                step_id="await-timer",
                iteration=-1,
                kind="timer",
                snapshot_version=1,
                claimed_event_id=None,
                claimed_at=1_030.0,
                expired=True,
            ),
        ]
        resumed = await WaitScheduler(engine, waits, TRUSTED_LOCAL).tick(1_031.0)
        assert resumed == ("no-such-run", outcome.run_id)


# --------------------------------------------------------------------------
# T-7 — sequential loops (child plan §4.7)
# --------------------------------------------------------------------------


def loop_artifact(
    *, failure_policy: str = "collect", max_iterations: int = 500
) -> Any:
    """The §6.1 loop, re-policied.  The V2 models are frozen, so a variant is
    rebuilt rather than mutated — which is also what a compiler change would
    do, so the fixture and the real path stay the same shape."""
    artifact = load_artifact("sequential-loop.artifact.json")
    loop = artifact.steps["for-each-task"]
    return with_step(
        artifact,
        "for-each-task",
        loop.model_copy(
            update={"failure_policy": failure_policy, "max_iterations": max_iterations}
        ),
    )


def loop_engine(
    *, failure_policy: str = "collect", max_iterations: int = 500
) -> tuple[PlaybookEngine, Any, RecordingRunRepository, Any]:
    artifact = loop_artifact(failure_policy=failure_policy, max_iterations=max_iterations)
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS)
    store = InMemoryArtifactStore()
    store.put(artifact)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry,
            clock=lambda: 1_000.0,
            artifact_store=store,
            bus=RecordingBus(),
        ),
        runs=runs,
        waits=None,
        activations=StubActivations([ref]),
    )
    return engine, adapter, runs, ref


def downstream(*ids: str) -> CommandResult:
    return CommandResult(
        outcome="listed",
        value=ListTasksResult(tasks=[{"id": task_id} for task_id in ids], count=len(ids)),
        summary="listed",
    )


def rejected(detail: str = "no") -> CommandResult:
    return CommandResult(
        outcome="rejected", value=EnsureTaskResult(task_id="", created=False), summary=detail
    )


class TestSequentialLoops:
    @pytest.mark.asyncio
    async def test_the_loop_runs_the_body_once_per_item_in_order(self):
        engine, adapter, _runs, ref = loop_engine()
        adapter.queue.extend([downstream("d-1", "d-2", "d-3"), ok(), ok(), ok()])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        gate_titles = [args.title for args in adapter.args_for("ensure_task")]
        assert gate_titles == ["Gate: d-1", "Gate: d-2", "Gate: d-3"]

    @pytest.mark.asyncio
    async def test_loop_item_lives_in_its_own_namespace(self):
        """A binding and a loop item may share a name and stay distinct.

        Against ``pipeline_runner``'s shape this is impossible: it wrote loop
        items into the same dict as step outputs, so a step output named
        ``task`` and an item named ``task`` silently collided.  Here they are
        different namespaces, and a ``BindingRef`` cannot reach ``loop``.
        """
        engine, adapter, runs, ref = loop_engine()
        adapter.queue.extend([downstream("d-1", "d-2"), ok(), ok()])
        await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        snapshot = next(iter(runs.snapshots.values()))
        artifact = loop_artifact()
        # Rebuild the scope the second iteration ran under.
        frame = LoopFrame(
            step_id="for-each-task",
            item_binding="task",
            collection_digest="ignored",
            index=1,
            total=2,
        )
        scope = engine._scope(replace(snapshot, loop=frame), artifact)
        assert scope.loop["task"] == {"id": "d-2"}
        assert scope.loop["task#index"] == 1
        assert "task" not in scope.bindings
        assert set(scope.bindings) >= {"downstream", "gate"}

    @pytest.mark.asyncio
    async def test_loop_item_is_not_visible_after_the_loop(self):
        engine, adapter, _runs, ref = loop_engine()
        adapter.queue.extend([downstream("d-1"), ok()])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.snapshot.loop is None
        assert engine._scope(outcome.snapshot, loop_artifact()).loop == {}

    @pytest.mark.asyncio
    async def test_frame_is_committed_on_both_sides_of_the_body(self):
        """Entering iteration *n* and leaving it are two boundaries.

        A crash mid-body therefore restarts iteration *n*, never *n+1*.
        """
        engine, adapter, runs, ref = loop_engine()
        adapter.queue.extend([downstream("d-1", "d-2", "d-3"), ok(), ok(), ok()])
        await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        loop_receipts = [r for r in runs.receipts if r.step_id == "for-each-task"]
        # 1 enter + 3 returns.
        assert len(loop_receipts) == 4
        assert [r.iteration for r in loop_receipts] == [-1, 0, 1, 2]
        # Every attempt identity is distinct, which is what the database's
        # uq_playbook_step_receipts_attempt enforces independently.
        keys = [(r.step_id, r.iteration, r.attempt) for r in runs.receipts]
        assert len(set(keys)) == len(keys)

    @pytest.mark.asyncio
    async def test_the_body_entry_edge_is_a_goto_not_a_transition(self):
        engine, adapter, runs, ref = loop_engine()
        adapter.queue.extend([downstream("d-1"), ok()])
        await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        entering = next(r for r in runs.receipts if r.step_id == "for-each-task")
        assert entering.selected_transition == transition_id(
            "sweep", "for-each-task", ITERATING_OUTCOME
        )

    @pytest.mark.asyncio
    async def test_empty_collection_goes_straight_to_continuation(self):
        engine, adapter, _runs, ref = loop_engine()
        adapter.queue.append(downstream())
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        assert adapter.names == ["list_tasks"]
        assert outcome.snapshot.bindings["sweep_result"] == {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "items": [],
        }

    @pytest.mark.asyncio
    async def test_non_list_collection_is_input_resolution_failed(self):
        """A reference that resolves to something other than a list.

        The compiler knows the shape of a *schema*, not the shape of a
        handler result, so this is the type error it could not see — and it
        is an outcome the artifact can route, not an exception.
        """
        artifact = loop_artifact()
        loop = artifact.steps["for-each-task"]
        artifact = with_step(
            artifact,
            "for-each-task",
            loop.model_copy(
                update={
                    "collection": BindingRef(binding="downstream", path="count"),
                }
            ),
        )
        ref = artifact_ref_for(artifact)
        registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS)
        store = InMemoryArtifactStore()
        store.put(artifact)
        runs = RecordingRunRepository()
        engine = PlaybookEngine(
            services=EngineServices(
                contracts=registry,
                clock=lambda: 1_000.0,
                artifact_store=store,
                bus=RecordingBus(),
            ),
            runs=runs,
            waits=None,
            activations=StubActivations([ref]),
        )
        adapter.queue.append(downstream("d-1"))
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.snapshot.current_step_id == "sweep-failed"
        assert adapter.names == ["list_tasks"]
        loop_receipt = [r for r in runs.receipts if r.step_id == "for-each-task"][-1]
        assert loop_receipt.error_code == "input_resolution_failed"
        assert "not a list" in (loop_receipt.error or "")

    @pytest.mark.asyncio
    async def test_a_collection_over_max_iterations_is_state_limit_exceeded(self):
        engine, adapter, runs, ref = loop_engine(max_iterations=2)
        adapter.queue.append(downstream("d-1", "d-2", "d-3"))
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.snapshot.current_step_id == "sweep-failed"
        assert adapter.names == ["list_tasks"]
        assert [r for r in runs.receipts if r.step_id == "for-each-task"][-1].error_code == (
            "state_limit_exceeded"
        )

    @pytest.mark.asyncio
    async def test_command_failure_edge_returning_to_the_loop_counts_as_failed(self):
        """§4.7's locked classification, on the failure side.

        ``rejected`` is a declared outcome the contract classifies FAILURE,
        and the artifact routes it *back into the loop node* — which is how an
        author says "this failure is per-item".
        """
        engine, adapter, _runs, ref = loop_engine(failure_policy="collect")
        adapter.queue.extend([downstream("d-1", "d-2"), rejected(), ok()])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        result = outcome.snapshot.bindings["sweep_result"]
        assert result["failed"] == 1
        assert result["succeeded"] == 1
        assert result["items"][0] == {
            "index": 0,
            "outcome": "rejected",
            "value": None,
            "error": "rejected",
        }
        assert result["items"][1]["error"] is None

    @pytest.mark.asyncio
    async def test_command_success_edge_returning_to_the_loop_counts_as_success(self):
        engine, adapter, _runs, ref = loop_engine()
        adapter.queue.extend([downstream("d-1"), ok()])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.snapshot.bindings["sweep_result"]["succeeded"] == 1
        assert outcome.snapshot.bindings["sweep_result"]["failed"] == 0

    @pytest.mark.parametrize(
        ("policy", "lifecycle", "step_id", "succeeded", "failed", "gate_calls"),
        [
            ("halt", RunLifecycle.FAILED, "sweep-failed", 0, 1, 1),
            ("continue", RunLifecycle.COMPLETED, "sweep-done", 3, 0, 3),
            ("collect", RunLifecycle.COMPLETED, "sweep-done", 2, 1, 3),
        ],
    )
    @pytest.mark.asyncio
    async def test_failure_policy_halt_continue_collect(
        self, policy, lifecycle, step_id, succeeded, failed, gate_calls
    ):
        """§4.7's table, one parameterisation per policy.

        ``continue`` records the iteration but not the error, so its aggregate
        reports no failures; ``collect`` keeps the error and still completes;
        only ``halt`` routes a per-item failure onto the loop's failed edge.
        """
        engine, adapter, _runs, ref = loop_engine(failure_policy=policy)
        adapter.queue.extend([downstream("d-1", "d-2", "d-3"), rejected(), ok(), ok()])
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.lifecycle is lifecycle
        assert outcome.snapshot.current_step_id == step_id
        assert len(adapter.args_for("ensure_task")) == gate_calls
        result = outcome.snapshot.bindings["sweep_result"]
        assert (result["succeeded"], result["failed"]) == (succeeded, failed)
        assert result["total"] == 3

    @pytest.mark.asyncio
    async def test_an_oversized_iteration_result_is_state_limit_exceeded(self):
        """The oversized value is rejected, never truncated into the loop.

        §4.7's prose says a ``collect`` over a large collection can end in
        ``state_limit_exceeded``; with Package 2's ``FOREACH_RESULT_SCHEMA``
        the aggregate carries no per-item ``value``, so the aggregate itself
        is bounded by ``max_iterations`` and the limit is reached through a
        body step's binding instead.  §2.5 item 13 records the difference.
        """
        engine, adapter, runs, ref = loop_engine()
        wide = "x" * 300_000
        adapter.queue.extend(
            [
                downstream("d-1"),
                CommandResult(
                    outcome="created",
                    value=EnsureTaskResult(task_id=wide, created=True),
                    summary="huge",
                ),
            ]
        )
        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)
        assert outcome.snapshot.current_step_id == "sweep-failed"
        gate = [r for r in runs.receipts if r.step_id == "open-gate"][-1]
        assert gate.error_code == "state_limit_exceeded"
        assert "gate" not in outcome.snapshot.bindings

    @pytest.mark.asyncio
    async def test_the_foreach_executor_is_identical_across_modes(self):
        live = executor_for("foreach", ExecutionMode.LIVE)
        assert live is executor_for("foreach", ExecutionMode.DRY_RUN)
        assert live is executor_for("foreach", ExecutionMode.SHADOW)
        assert live.no_side_effects is True

    @pytest.mark.asyncio
    async def test_a_second_loop_while_one_is_active_is_a_contract_violation(self):
        """Package 2 rejects a *nested* loop statically; this is the dynamic
        form of the same thing — two frames, one snapshot field."""
        from src.playbooks.executors.foreach import ForEachExecutor

        artifact = loop_artifact()
        step = artifact.steps["for-each-task"]
        ctx = StepContext(
            run_id="r",
            dispatch_id="d",
            artifact_ref=artifact_ref_for(artifact),
            artifact=artifact,
            rule_id="sweep",
            step_id="for-each-task",
            principal=TRUSTED_LOCAL,
            scope=ResolutionScope(),
            services=EngineServices(contracts=None, clock=lambda: 0.0),
            loop_frame=LoopFrame(
                step_id="another-loop",
                item_binding="item",
                collection_digest="d",
                index=0,
                total=1,
            ),
        )
        result = await ForEachExecutor().execute(step, ctx)
        assert result.outcome == "contract_violation"

    @pytest.mark.asyncio
    async def test_a_collection_that_changed_under_an_active_loop_is_refused(self):
        from src.playbooks.executors.foreach import ForEachExecutor

        artifact = loop_artifact()
        step = artifact.steps["for-each-task"]
        ctx = StepContext(
            run_id="r",
            dispatch_id="d",
            artifact_ref=artifact_ref_for(artifact),
            artifact=artifact,
            rule_id="sweep",
            step_id="for-each-task",
            principal=TRUSTED_LOCAL,
            scope=ResolutionScope(bindings={"downstream": {"tasks": [{"id": "d-9"}]}}),
            services=EngineServices(contracts=None, clock=lambda: 0.0),
            loop_frame=LoopFrame(
                step_id="for-each-task",
                item_binding="task",
                collection_digest="sha256:stale",
                index=0,
                total=1,
            ),
        )
        result = await ForEachExecutor().execute(step, ctx)
        assert result.outcome == "contract_violation"
        assert "changed" in result.diagnostics[0]
