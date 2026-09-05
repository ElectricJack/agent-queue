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

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL
from src.config import LLMConfig
from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.llm.types import TokenUsage
from src.playbooks.definition import LlmStep, load_definition_json
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
from src.playbooks.executors.llm import _published_tools, resolve_profile_principal
from src.playbooks.executors.wait import UNRESOLVED_REASON
from src.playbooks.expressions import BindingRef, EventRef, Exists, ResolutionScope
from src.playbooks.receipts import RECEIPT_OUTCOMES, transition_id
from src.playbooks.run_state import (
    LoopFrame,
    RunLifecycle,
    SnapshotVersionConflict,
    StateLimitExceeded,
)
from src.playbooks.waits import (
    EMPTY_WAIT_CHANGES,
    WaitClaim,
    WaitRegistration,
    WaitSpec,
)
from src.profiles.capabilities import CapabilityPolicy
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
    FIXTURES,
    InMemoryArtifactStore,
    RecordingBus,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
    event,
    load_artifact,
    minimal_artifact,
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
    artifact: Any | None = None,
) -> tuple[PlaybookEngine, Any, RecordingRunRepository, RecordingBus, Any]:
    artifact = load_artifact(artifact_name) if artifact is None else artifact
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


class _Resolver:
    def is_builtin(self, _name: str) -> bool:
        return True

    def is_plugin(self, _name: str) -> bool:
        return False

    def plugin_command_names(self) -> frozenset[str]:
        return frozenset()


class _ProfileStore:
    async def get_profile(self, _profile_id: str) -> Any:
        return SimpleNamespace(
            id="worker",
            allowed_tools=[],
            harness_tools=[],
            aq_commands=["ensure_task"],
            plugin_tools=[],
        )


def _llm_step() -> LlmStep:
    return LlmStep.model_validate(
        {
            "rule": "r",
            "title": "Classify",
            "source": {"path": "x.md", "start_line": 1, "end_line": 1},
            "profile_id": "worker",
            "prompt": {"type": "literal", "value": "classify"},
            "output_schema": {
                "type": "object",
                "properties": {"risk": {"type": "string", "enum": ["low", "high"]}},
                "required": ["risk"],
                "additionalProperties": False,
            },
            "outcome_field": "risk",
            "tool_use": {"enabled": True, "aq_commands": ["ensure_task"]},
            "budget": {
                "max_calls": 3,
                "max_output_tokens": 256,
                "max_total_tokens": 8000,
                "timeout_seconds": 60,
            },
            "save_result_as": "classification",
            "transitions": {
                "low": "done",
                "high": "done",
                "invalid_output": "bad",
                "budget_exceeded": "bad",
                "provider_error": "bad",
                "runtime_error": "bad",
            },
        }
    )


@pytest.mark.asyncio
async def test_trusted_service_llm_is_narrowed_to_an_enforced_profile_principal():
    resolution = await resolve_profile_principal(
        _llm_step(),
        SimpleNamespace(db=_ProfileStore(), resolver=_Resolver()),
        ExecutionPrincipal.service("timer"),
    )

    assert resolution.principal is not None
    assert resolution.principal.kind is PrincipalKind.PLAYBOOK
    assert resolution.principal.profile_id == "worker"
    assert resolution.principal.policy.aq_commands == frozenset({"ensure_task"})


def test_contracted_plugin_tools_are_published_under_the_plugin_namespace():
    from src.commands.contracts import CONTRACTS

    class PluginResolver:
        def is_builtin(self, _name: str) -> bool:
            return False

        def is_plugin(self, name: str) -> bool:
            return name == "git_diff"

        def plugin_command_names(self) -> frozenset[str]:
            return frozenset({"git_diff"})

    step = _llm_step().model_copy(
        update={
            "tool_use": _llm_step().tool_use.model_copy(
                update={"aq_commands": [], "plugin_tools": ["git_diff"]}
            )
        }
    )
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK,
        policy=CapabilityPolicy.from_namespaces(plugin_tools=["git_diff"]),
    )
    ctx = SimpleNamespace(
        principal=principal,
        services=SimpleNamespace(
            contracts=CONTRACTS,
            resolver=PluginResolver(),
            authorization_mode="enforce",
        ),
    )

    assert [tool["name"] for tool in _published_tools(step, ctx)] == ["git_diff"]


def build_llm(
    provider: FakeProvider,
    *,
    runs: RecordingRunRepository | None = None,
    step: LlmStep | None = None,
):
    artifact = minimal_artifact()
    artifact = with_step(artifact, "ensure-review-task", step or _llm_step())
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(ENSURE_TASK)
    store = InMemoryArtifactStore()
    store.put(artifact)
    runs = runs or RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=registry,
            clock=lambda: 1_000.0,
            artifact_store=store,
            llm=LLMClient.with_provider(provider, config=LLMConfig()),
            db=_ProfileStore(),
            resolver=_Resolver(),
            authorization_mode="enforce",
        ),
        runs=runs,
        waits=None,
        activations=StubActivations([ref]),
    )
    return engine, adapter, runs, ref


TOOL_PRINCIPAL = ExecutionPrincipal(
    kind=PrincipalKind.SESSION,
    policy=CapabilityPolicy.from_namespaces(
        aq_commands=["ensure_task", "playbook_admin"]
    ),
)


def _ensure_step_with(*, inputs: dict[str, Any] | None = None, idempotency_key: Any = None):
    """``two-rules-one-event``, with authored keying on its ``ensure_task``.

    Edited as JSON and re-loaded rather than ``model_copy``-ed, so the added
    values go through the same validation a compiled artifact does.
    """
    raw = json.loads((FIXTURES / "two-rules-one-event.artifact.json").read_text())
    step = raw["steps"]["ensure-review-task"]
    step["inputs"].update(inputs or {})
    if idempotency_key is not None:
        step["idempotency_key"] = idempotency_key
    return load_definition_json(json.dumps(raw))


class TestAuthoredIdempotencyKey:
    """The authored key reaches the command; the attempt key reaches the receipt.

    These two live at the engine level rather than only in the executor suite
    because the step-level override has to be *resolved* against the run's
    scope before an executor can see it, and that resolution is the engine's
    job — the same one that turns a miss into ``input_resolution_failed``
    before any side effect happens.
    """

    @pytest.mark.asyncio
    async def test_an_authored_dedup_key_reaches_the_command_unchanged(self):
        artifact = _ensure_step_with(
            inputs={
                "dedup_key": {
                    "type": "template",
                    "parts": [
                        {"type": "literal", "value": "review:task:"},
                        {"type": "event_ref", "path": "task_id"},
                    ],
                }
            }
        )
        engine, adapter, runs, _bus, _ref = build(artifact=artifact)
        adapter.queue.extend([ok(), listed()])

        result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)

        assert adapter.args_for("ensure_task")[0].dedup_key == "review:task:task-1"
        receipts = [
            r
            for run_id in result.run_ids
            for r in runs.receipts
            if r.run_id == run_id and r.step_id == "ensure-review-task"
        ]
        assert receipts
        # Attempt identity is still recorded, and it is not the argument.
        assert all(r.idempotency_key.endswith(":ensure-review-task:-:1") for r in receipts)

    @pytest.mark.asyncio
    async def test_a_step_level_override_is_resolved_and_wins(self):
        artifact = _ensure_step_with(
            inputs={"dedup_key": {"type": "literal", "value": "authored-input"}},
            idempotency_key={
                "type": "template",
                "parts": [
                    {"type": "literal", "value": "review-of-"},
                    {"type": "event_ref", "path": "task_id"},
                ],
            },
        )
        engine, adapter, _runs, _bus, _ref = build(artifact=artifact)
        adapter.queue.extend([ok(), listed()])

        await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)

        assert adapter.args_for("ensure_task")[0].dedup_key == "review-of-task-1"

    @pytest.mark.asyncio
    async def test_an_unresolvable_step_level_key_fails_before_the_command_runs(self):
        artifact = _ensure_step_with(
            idempotency_key={"type": "event_ref", "path": "missing_field"},
        )
        engine, adapter, runs, _bus, _ref = build(artifact=artifact)
        adapter.queue.extend([ok(), listed()])

        result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)

        assert adapter.args_for("ensure_task") == []
        outcomes = {
            r.outcome
            for run_id in result.run_ids
            for r in runs.receipts
            if r.run_id == run_id and r.step_id == "ensure-review-task"
        }
        assert outcomes == {"failure"}


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
    async def test_a_false_rule_guard_rejects_the_rule(self):
        """§4.2 — the guard is V1's ``when`` clause, evaluated before dispatch.

        V1 skipped a rule whose ``when`` was false (``core.py`` ``_eval_pipeline_when``).
        Selecting it here would start a run V1 never started, which Package 6's
        parity harness reads as a rule-selection difference.
        """
        engine, adapter, _runs, _bus, ref = build()
        artifact = engine.services.artifact_store.load(ref.artifact_sha256)
        guarded = artifact.rules[0].model_copy(
            update={
                "guard": Exists(
                    type="exists",
                    value=EventRef(type="event_ref", path="task.pr_url"),
                    mode="truthy",
                )
            }
        )
        artifact = artifact.model_copy(update={"rules": [guarded, *artifact.rules[1:]]})
        engine.services.artifact_store.put(artifact)
        engine.activations = StubActivations([artifact_ref_for(artifact)])
        adapter.queue.append(listed())

        result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)

        assert guarded.id not in result.rules_selected

    @pytest.mark.asyncio
    async def test_a_true_rule_guard_keeps_the_rule(self):
        engine, adapter, _runs, _bus, ref = build()
        artifact = engine.services.artifact_store.load(ref.artifact_sha256)
        guarded = artifact.rules[0].model_copy(
            update={
                "guard": Exists(
                    type="exists",
                    value=EventRef(type="event_ref", path="task_id"),
                    mode="truthy",
                )
            }
        )
        artifact = artifact.model_copy(update={"rules": [guarded, *artifact.rules[1:]]})
        engine.services.artifact_store.put(artifact)
        engine.activations = StubActivations([artifact_ref_for(artifact)])
        adapter.queue.extend([ok(), listed()])

        result = await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)

        assert guarded.id in result.rules_selected

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
        """§2.5 item 9 — replay collides on the shipped playbook-scoped
        dispatch/rule index rather than on a pre-read."""
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
        # The command attempt is fenced by a receipted start boundary before
        # it runs; the transition step needs no fence.
        assert [(r.step_id, r.receipt_kind) for r in runs.receipts] == [
            ("ensure-review-task", "attempt_start"),
            ("ensure-review-task", "step"),
            ("review-done", "step"),
        ]

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
            step = next(
                r
                for r in runs.receipts
                if r.run_id == outcome.run_id and r.receipt_kind == "step"
            )
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
    async def test_exactly_one_commit_per_durable_boundary(self):
        engine, adapter, runs, _bus, _ref = build()
        adapter.queue.extend([ok(), listed()])
        await engine.dispatch_event(event("task-completed-code"), TRUSTED_LOCAL)
        assert runs.commit_calls == len(runs.receipts)
        identities = [
            (
                r.run_id,
                r.step_id,
                r.iteration,
                r.attempt,
                r.turn_index,
                r.receipt_kind,
            )
            for r in runs.receipts
        ]
        assert len(identities) == len(set(identities))

    @pytest.mark.asyncio
    async def test_no_side_effect_happens_before_the_attempt_start_boundary(self):
        """The fence is a boundary of its own: if it cannot be written, the
        command never runs, so a side effect can never exist without the
        durable record that its attempt began."""
        runs = RecordingRunRepository(fail_commit_with=RuntimeError("db down"))
        engine, adapter, runs, bus, ref = build(runs=runs)
        adapter.queue.append(ok())
        with pytest.raises(RuntimeError):
            await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert adapter.names == []
        assert runs.receipts == []
        assert [name for name, _ in bus.events] == ["playbook.v2.run.started"]

    @pytest.mark.asyncio
    async def test_no_durable_write_happens_between_the_fence_and_the_boundary(self):
        """The side effect is real; the snapshot did not move past the fence
        and no step event was emitted.  Proves the §3.4 step-11 ordering."""

        class FailAfterFence(RecordingRunRepository):
            async def commit_boundary(self, snapshot, receipt, wait_changes=None):
                if receipt.receipt_kind != "attempt_start":
                    raise RuntimeError("db down")
                return await super().commit_boundary(snapshot, receipt, wait_changes)

        runs = FailAfterFence()
        engine, adapter, runs, bus, ref = build(runs=runs)
        adapter.queue.append(ok())
        with pytest.raises(RuntimeError):
            await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert adapter.names == ["ensure_task"]
        # Exactly the fence landed: one version, one receipt, and the marker
        # recovery reads is on the snapshot the fence wrote.
        assert [r.receipt_kind for r in runs.receipts] == ["attempt_start"]
        run_id = next(iter(runs.snapshots))
        assert runs.snapshots[run_id].version == 1
        assert runs.receipts[0].snapshot_version == 1
        assert runs.snapshots[run_id].context["_in_flight_attempt"]["step_id"] == (
            "ensure-review-task"
        )
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
        # One event per *completed* step; the attempt-start fence completes
        # nothing and announces nothing.
        assert kinds.count("playbook.v2.step.completed") == len(
            [r for r in runs.receipts if r.receipt_kind == "step"]
        )
        assert kinds[0] == "playbook.v2.run.started"
        assert kinds[-1] == "playbook.v2.run.finished"


class TestLlmToolTurnBoundaries:
    @pytest.mark.asyncio
    async def test_tool_enabled_provider_deadline_is_timed_out_not_interrupted(
        self, monkeypatch
    ):
        class BlockingProvider(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **_kwargs):
                await asyncio.Event().wait()

        real_timeout = asyncio.timeout
        monkeypatch.setattr(
            "src.llm.client.asyncio.timeout", lambda _seconds: real_timeout(0.01)
        )
        engine, _adapter, runs, ref = build_llm(BlockingProvider())

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert [receipt.receipt_kind for receipt in runs.receipts] == [
            "attempt_start",
            "step",
            "step",
        ]
        assert runs.receipts[1].error_code == "timed_out"
        assert "interrupted" not in {receipt.receipt_kind for receipt in runs.receipts}

    @pytest.mark.asyncio
    async def test_turn_boundary_size_failure_is_not_a_provider_error(self):
        class RejectFirstBoundary(RecordingRunRepository):
            async def commit_boundary(self, snapshot, receipt, wait_changes=None):
                if receipt.receipt_kind == "tool_turn" and self.commit_calls == 1:
                    self.commit_calls += 1
                    raise StateLimitExceeded(
                        snapshot.run_id, receipt.step_id, "tool result", 300_000, 262_144
                    )
                return await super().commit_boundary(snapshot, receipt, wait_changes)

        class UsageProvider(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

        provider = UsageProvider()
        provider.add_tool_call("ensure_task", {"project_id": "p", "title": "one"})
        runs = RejectFirstBoundary()
        engine, adapter, _runs, ref = build_llm(provider, runs=runs)
        adapter.queue.append(ok())

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert [receipt.receipt_kind for receipt in runs.receipts][:2] == [
            "attempt_start",
            "step",
        ]
        assert runs.receipts[1].error_code == "state_limit_exceeded"
        assert "provider_error" not in {receipt.error_code for receipt in runs.receipts}

    @pytest.mark.asyncio
    async def test_turn_boundary_version_conflict_stops_without_another_provider_call(self):
        provider = FakeProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        # Commit 1 is the attempt-start fence; the tool turn is commit 2.
        runs = RecordingRunRepository(conflict_on_commit=2)
        engine, adapter, _runs, ref = build_llm(provider, runs=runs)
        adapter.queue.append(ok())

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert outcome.outcome == "interrupted"
        assert outcome.snapshot is not None and outcome.snapshot.version == 2
        assert len(provider.calls) == 1
        assert runs.commit_calls == 3
        assert [receipt.error_code for receipt in runs.receipts] == [None, "interrupted"]

    @pytest.mark.asyncio
    async def test_cancellation_wins_a_race_with_a_tool_turn_boundary(self):
        class CasBoundaryRepository(RecordingRunRepository):
            def __init__(self):
                super().__init__()
                self.boundary_started = asyncio.Event()
                self.release_boundary = asyncio.Event()
                self.cancel_written = asyncio.Event()

            async def commit_boundary(self, snapshot, receipt, wait_changes=EMPTY_WAIT_CHANGES):
                if receipt.receipt_kind == "tool_turn":
                    self.boundary_started.set()
                    await self.release_boundary.wait()
                current = self.snapshots[snapshot.run_id]
                if current.version != snapshot.version:
                    raise SnapshotVersionConflict(
                        snapshot.run_id, snapshot.version, current.version
                    )
                return await super().commit_boundary(snapshot, receipt, wait_changes)

            async def request_cancel(self, *args, **kwargs):
                snapshot = await super().request_cancel(*args, **kwargs)
                self.cancel_written.set()
                return snapshot

        provider = FakeProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        provider.add_text('{"risk":"low"}', usage=TokenUsage(5, 1, True))
        runs = CasBoundaryRepository()
        engine, adapter, _runs, ref = build_llm(provider, runs=runs)
        engine.cancellation_grace_seconds = 0.01
        adapter.queue.append(ok())

        walk = asyncio.create_task(engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL))
        await runs.boundary_started.wait()
        run_id = next(iter(runs.snapshots))
        cancelling = asyncio.create_task(engine.cancel(run_id, TOOL_PRINCIPAL))
        # Hold the completed turn in storage beyond the grace interval.  Its
        # callback owns the per-run writer lock, so cancellation intent (and
        # therefore the grace clock) cannot overtake it.
        await asyncio.sleep(0.03)
        assert not runs.cancel_written.is_set()
        runs.release_boundary.set()

        outcome, cancelled = await asyncio.gather(walk, cancelling)

        assert outcome.lifecycle is RunLifecycle.CANCELLED
        assert cancelled.lifecycle is RunLifecycle.CANCELLED
        assert runs.snapshots[run_id].lifecycle is RunLifecycle.CANCELLED
        assert [receipt.receipt_kind for receipt in runs.receipts] == [
            "attempt_start",
            "tool_turn",
            "step",
        ]
        assert [receipt.error_code for receipt in runs.receipts] == [None, None, "cancelled"]
        assert runs.receipts[2].result["cancellation"] == "acknowledged"
        assert runs.snapshots[run_id].budget.llm_calls == 1

    @pytest.mark.asyncio
    async def test_grace_expiry_stops_new_tool_dispatch_after_provider_returns(self):
        class HeldProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **kwargs):
                self.entered.set()
                await self.release.wait()
                return await super().create_message(**kwargs)

        provider = HeldProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "must-not-run"},
            usage=TokenUsage(10, 2, True),
        )
        engine, adapter, runs, ref = build_llm(provider)
        engine.cancellation_grace_seconds = 0.01

        walk = asyncio.create_task(engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL))
        await provider.entered.wait()
        run_id = next(iter(runs.snapshots))
        cancelled = await engine.cancel(run_id, TOOL_PRINCIPAL)

        assert cancelled.lifecycle is RunLifecycle.CANCELLED
        assert adapter.calls == []
        provider.release.set()
        outcome = await walk

        assert outcome.lifecycle is RunLifecycle.CANCELLED
        assert adapter.calls == []
        assert [r.error_code for r in runs.receipts] == [None, "cancelled"]

    @pytest.mark.asyncio
    async def test_pending_cancel_is_committed_when_initial_read_loses_scheduler(self):
        class DelayedCancelReadRepository(RecordingRunRepository):
            def __init__(self):
                super().__init__()
                self.delay_read = False
                self.read_started = asyncio.Event()
                self.release_read = asyncio.Event()

            async def load_run(self, run_id):
                if self.delay_read:
                    self.delay_read = False
                    self.read_started.set()
                    await self.release_read.wait()
                return await super().load_run(run_id)

        class HeldProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **kwargs):
                self.entered.set()
                await self.release.wait()
                return await super().create_message(**kwargs)

        provider = HeldProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "must-not-run"},
            usage=TokenUsage(10, 2, True),
        )
        runs = DelayedCancelReadRepository()
        engine, adapter, _runs, ref = build_llm(provider, runs=runs)
        walk = asyncio.create_task(engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL))
        await provider.entered.wait()
        run_id = next(iter(runs.snapshots))

        runs.delay_read = True
        cancelling = asyncio.create_task(engine.cancel(run_id, TOOL_PRINCIPAL))
        await runs.read_started.wait()
        provider.release.set()
        outcome = await walk
        runs.release_read.set()
        cancelled = await cancelling

        assert outcome.lifecycle is RunLifecycle.CANCELLED
        assert cancelled.lifecycle is RunLifecycle.CANCELLED
        assert adapter.calls == []
        assert [r.error_code for r in runs.receipts] == [None, "cancelled"]

    @pytest.mark.asyncio
    async def test_multi_turn_tool_loop_commits_one_receipt_per_completed_turn(self):
        provider = FakeProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "two"},
            usage=TokenUsage(11, 3, True),
        )
        provider.add_text('{"risk":"high"}', usage=TokenUsage(12, 4, True))
        engine, adapter, runs, ref = build_llm(provider)
        adapter.queue.extend([ok("t-1"), ok("t-2")])

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        turns = [r for r in runs.receipts if r.receipt_kind == "tool_turn"]
        assert [(r.turn_index, r.tokens_in, r.tokens_out) for r in turns] == [
            (0, 10, 2),
            (1, 11, 3),
        ]
        assert len({r.idempotency_key for r in turns}) == 1
        # Version 1 is the attempt-start fence.
        assert [r.snapshot_version for r in turns] == [2, 3]
        assert {r.principal["profile_id"] for r in turns} == {"worker"}
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        assert runs.snapshots[outcome.run_id].budget.llm_calls == 3
        assert runs.snapshots[outcome.run_id].budget.total_tokens == 42

    @pytest.mark.asyncio
    async def test_tool_turn_at_call_limit_does_not_invent_a_schema_retry_call(self):
        provider = FakeProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        step = _llm_step()
        step = step.model_copy(
            update={"budget": step.budget.model_copy(update={"max_calls": 1})}
        )
        engine, adapter, runs, ref = build_llm(provider, step=step)
        adapter.queue.append(ok())

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert [r.receipt_kind for r in runs.receipts] == [
            "attempt_start",
            "tool_turn",
            "step",
            "step",
        ]
        assert runs.receipts[2].error_code == "budget_exceeded"
        assert runs.snapshots[outcome.run_id].budget.llm_calls == 1

    @pytest.mark.asyncio
    async def test_interrupted_tool_call_receipts_and_pauses_without_a_final_step_receipt(self):
        provider = FakeProvider()
        provider.add_tool_calls(
            [
                ("ensure_task", {"project_id": "p", "title": "one"}),
                ("ensure_task", {"project_id": "p", "title": "two"}),
            ],
            usage=TokenUsage(10, 2, True),
        )
        engine, adapter, runs, ref = build_llm(provider)
        adapter.queue.extend([ok("t-1"), asyncio.CancelledError()])

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert outcome.outcome == "operator_decision_required"
        assert [r.receipt_kind for r in runs.receipts] == ["attempt_start", "interrupted"]
        receipt = runs.receipts[1]
        assert receipt.operator_decision_id
        assert receipt.selected_transition is None
        assert runs.snapshots[outcome.run_id].operator_decision is not None

    @pytest.mark.asyncio
    async def test_retry_after_restart_continues_after_the_last_committed_turn(self):
        class ProcessCrash(BaseException):
            pass

        class CrashAfterFirstResponse(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **kwargs):
                if self.calls:
                    raise ProcessCrash
                return await super().create_message(**kwargs)

        first_provider = CrashAfterFirstResponse()
        first_provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        engine, adapter, runs, ref = build_llm(first_provider)
        adapter.queue.append(ok("t-1"))
        with pytest.raises(ProcessCrash):
            await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)
        run_id = next(iter(runs.snapshots))
        assert runs.snapshots[run_id].lifecycle is RunLifecycle.RUNNING
        assert [r.receipt_kind for r in runs.receipts] == ["attempt_start", "tool_turn"]

        resumed_provider = FakeProvider()
        resumed_provider.add_text('{"risk":"low"}', usage=TokenUsage(5, 1, True))
        restarted, restarted_adapter, _, _ = build_llm(resumed_provider, runs=runs)
        interrupted = await restarted.resume(
            run_id,
            EventArrived(event_id="recovery"),
            TOOL_PRINCIPAL,
        )
        assert interrupted.lifecycle is RunLifecycle.PAUSED
        assert resumed_provider.calls == []
        assert [r.receipt_kind for r in runs.receipts] == [
            "attempt_start",
            "tool_turn",
            "interrupted",
        ]
        assert runs.receipts[-1].principal["profile_id"] == "worker"
        # The interruption closed the attempt the fence opened.
        assert "_in_flight_attempt" not in runs.snapshots[run_id].context

        resumed = await restarted.resume(
            run_id,
            OperatorResolution(kind="retry"),
            TOOL_PRINCIPAL,
        )

        assert resumed.lifecycle is RunLifecycle.COMPLETED
        assert restarted_adapter.calls == []
        assert len(resumed_provider.calls) == 1
        messages = resumed_provider.calls[0].messages
        assert [message["role"] for message in messages[-2:]] == ["assistant", "user"]
        # The retry continues the *same* attempt, so its fence is the second
        # start of attempt 1 — receipted at the next start ordinal rather
        # than colliding with the first or inventing a new attempt.
        assert [(r.receipt_kind, r.attempt, r.turn_index) for r in runs.receipts[:6]] == [
            ("attempt_start", 1, 0),
            ("tool_turn", 1, 0),
            ("interrupted", 1, 1),
            ("operator_decision", 1, 1),
            ("attempt_start", 1, 1),
            ("step", 1, -1),
        ]

    @pytest.mark.asyncio
    async def test_restart_recovery_does_not_fall_back_to_the_broader_caller(self):
        class ProcessCrash(BaseException):
            pass

        class CrashProvider(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **_kwargs):
                raise ProcessCrash

        class MissingProfileStore:
            async def get_profile(self, _profile_id):
                return None

        engine, _adapter, runs, ref = build_llm(CrashProvider())
        with pytest.raises(ProcessCrash):
            await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)
        run_id = next(iter(runs.snapshots))

        restarted, _adapter, _runs, _ref = build_llm(FakeProvider(), runs=runs)
        restarted.services = replace(restarted.services, db=MissingProfileStore())
        outcome = await restarted.resume(
            run_id, EventArrived(event_id="recovery"), TOOL_PRINCIPAL
        )

        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert runs.receipts[-1].receipt_kind == "interrupted"
        assert runs.receipts[-1].principal == {
            "kind": None,
            "profile_id": None,
            "session_id": None,
            "capability_fingerprint": "",
        }

    @pytest.mark.asyncio
    async def test_interrupted_call_counts_against_the_restart_call_budget(self):
        class ProcessCrash(BaseException):
            pass

        class CrashAfterToolTurn(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **kwargs):
                if self.calls:
                    raise ProcessCrash
                return await super().create_message(**kwargs)

        step = _llm_step()
        step = step.model_copy(
            update={"budget": step.budget.model_copy(update={"max_calls": 2})}
        )
        first = CrashAfterToolTurn()
        first.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        engine, adapter, runs, ref = build_llm(first, step=step)
        adapter.queue.append(ok())
        with pytest.raises(ProcessCrash):
            await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)
        run_id = next(iter(runs.snapshots))

        resumed_provider = FakeProvider()
        resumed_provider.add_text('{"risk":"low"}', usage=TokenUsage(5, 1, True))
        restarted, _adapter, _runs, _ref = build_llm(
            resumed_provider, runs=runs, step=step
        )
        await restarted.resume(run_id, EventArrived(event_id="recovery"), TOOL_PRINCIPAL)
        outcome = await restarted.resume(
            run_id, OperatorResolution(kind="retry"), TOOL_PRINCIPAL
        )

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert resumed_provider.calls == []
        llm_receipts = [r for r in runs.receipts if r.step_kind == "llm"]
        assert llm_receipts[-1].error_code == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_interrupted_usage_counts_against_the_restart_token_budget(self):
        step = _llm_step()
        step = step.model_copy(
            update={"budget": step.budget.model_copy(update={"max_total_tokens": 10})}
        )
        first_provider = FakeProvider()
        first_provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(9, 2, True),
        )
        first_engine, adapter, runs, ref = build_llm(first_provider, step=step)
        adapter.queue.append(asyncio.CancelledError())
        paused = await first_engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)
        assert paused.lifecycle is RunLifecycle.PAUSED

        resumed_provider = FakeProvider()
        resumed_provider.add_text('{"risk":"low"}', usage=TokenUsage(1, 1, True))
        restarted, _adapter, _runs, _ref = build_llm(
            resumed_provider, runs=runs, step=step
        )
        outcome = await restarted.resume(
            paused.run_id, OperatorResolution(kind="retry"), TOOL_PRINCIPAL
        )

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert resumed_provider.calls == []
        llm_receipts = [r for r in runs.receipts if r.step_kind == "llm"]
        assert llm_receipts[-1].error_code == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_retry_can_be_interrupted_twice_without_reusing_turn_identity(self):
        class CancelProvider(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **_kwargs):
                raise asyncio.CancelledError

        first_engine, _adapter, runs, ref = build_llm(CancelProvider())
        first = await first_engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)
        assert first.lifecycle is RunLifecycle.PAUSED

        second_engine, _adapter, _runs, _ref = build_llm(CancelProvider(), runs=runs)
        second = await second_engine.resume(
            first.run_id, OperatorResolution(kind="retry"), TOOL_PRINCIPAL
        )

        assert second.lifecycle is RunLifecycle.PAUSED
        interrupted = [r for r in runs.receipts if r.receipt_kind == "interrupted"]
        assert [r.turn_index for r in interrupted] == [0, 1]
        assert len({r.operator_decision_id for r in interrupted}) == 2

        final_provider = FakeProvider()
        final_provider.add_text('{"risk":"low"}', usage=TokenUsage(5, 1, True))
        final_engine, _adapter, _runs, _ref = build_llm(final_provider, runs=runs)
        final = await final_engine.resume(
            first.run_id, OperatorResolution(kind="retry"), TOOL_PRINCIPAL
        )
        assert final.lifecycle is RunLifecycle.COMPLETED

    @pytest.mark.asyncio
    async def test_schema_retry_carries_transcript_and_advances_tool_turn_index(self):
        step = _llm_step()
        step = step.model_copy(
            update={"budget": step.budget.model_copy(update={"max_calls": 5})}
        )
        provider = FakeProvider()
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(10, 2, True),
        )
        provider.add_text("not json", usage=TokenUsage(5, 1, True))
        provider.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "two"},
            usage=TokenUsage(11, 3, True),
        )
        provider.add_text('{"risk":"low"}', usage=TokenUsage(6, 1, True))
        engine, adapter, runs, ref = build_llm(provider, step=step)
        adapter.queue.extend([ok("t-1"), ok("t-2")])

        outcome = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert outcome.lifecycle is RunLifecycle.COMPLETED
        assert [
            r.turn_index for r in runs.receipts if r.receipt_kind == "tool_turn"
        ] == [0, 2]
        assert [
            (r.receipt_kind, r.turn_index)
            for r in runs.receipts
            if r.receipt_kind != "step"
        ] == [("attempt_start", 0), ("tool_turn", 0), ("llm_call", 1), ("tool_turn", 2)]
        assert all(r.outcome == "success" for r in runs.receipts[1:-1])
        retry_messages = provider.calls[2].messages
        assert [message["role"] for message in retry_messages[-2:]] == [
            "assistant",
            "user",
        ]
        assert "Return only JSON" in retry_messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_schema_retry_calls_remain_budgeted_after_interruption_and_restart(self):
        class CancelThirdCall(FakeProvider):
            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **kwargs):
                if len(self.calls) == 2:
                    raise asyncio.CancelledError
                return await super().create_message(**kwargs)

        first = CancelThirdCall()
        first.add_tool_call(
            "ensure_task",
            {"project_id": "p", "title": "one"},
            usage=TokenUsage(1, 0, True),
        )
        first.add_text("not json", usage=TokenUsage(1, 0, True))
        engine, adapter, runs, ref = build_llm(first)
        adapter.queue.append(ok())

        paused = await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        assert paused.lifecycle is RunLifecycle.PAUSED
        assert [r.receipt_kind for r in runs.receipts] == [
            "attempt_start",
            "tool_turn",
            "llm_call",
            "interrupted",
        ]
        assert runs.snapshots[paused.run_id].budget.llm_calls == 3
        assert runs.snapshots[paused.run_id].budget.total_tokens == 2

        resumed_provider = FakeProvider()
        resumed_provider.add_text('{"risk":"low"}', usage=TokenUsage(1, 0, True))
        restarted, _adapter, _runs, _ref = build_llm(resumed_provider, runs=runs)
        outcome = await restarted.resume(
            paused.run_id, OperatorResolution(kind="retry"), TOOL_PRINCIPAL
        )

        assert outcome.lifecycle is RunLifecycle.FAILED
        assert resumed_provider.calls == []
        assert [r.error_code for r in runs.receipts if r.step_kind == "llm"][-1] == (
            "budget_exceeded"
        )

    @pytest.mark.asyncio
    async def test_schema_retry_without_tools_is_durable_before_second_call(self):
        class ProcessCrash(BaseException):
            pass

        class CrashSecondCall(FakeProvider):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            @property
            def reports_usage(self) -> bool:
                return True

            async def create_message(self, **kwargs):
                self.attempts += 1
                if self.attempts == 2:
                    raise ProcessCrash
                return await super().create_message(**kwargs)

        step = _llm_step()
        step = step.model_copy(
            update={
                "tool_use": step.tool_use.model_copy(
                    update={"enabled": False, "aq_commands": []}
                )
            }
        )
        provider = CrashSecondCall()
        provider.add_text("not json", usage=TokenUsage(2, 1, True))
        engine, _adapter, runs, ref = build_llm(provider, step=step)

        with pytest.raises(ProcessCrash):
            await engine.run_rule(ref, "r", {}, TOOL_PRINCIPAL)

        run_id = next(iter(runs.snapshots))
        assert provider.attempts == 2
        assert [r.receipt_kind for r in runs.receipts] == ["attempt_start", "llm_call"]
        assert runs.snapshots[run_id].budget.llm_calls == 1
        assert runs.snapshots[run_id].budget.total_tokens == 3

        restarted, _adapter, _runs, _ref = build_llm(
            FakeProvider(), runs=runs, step=step
        )
        await restarted.resume(
            run_id, EventArrived(event_id="recovery"), TOOL_PRINCIPAL
        )
        assert [r.receipt_kind for r in runs.receipts] == [
            "attempt_start",
            "llm_call",
            "interrupted",
        ]
        assert runs.snapshots[run_id].budget.llm_calls == 2


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
        first = next(r for r in runs.receipts if r.receipt_kind == "step")
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
            ("blocked", RunLifecycle.BLOCKED),
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

    @pytest.mark.asyncio
    async def test_blocked_terminal_returns_and_emits_the_blocked_outcome(self):
        blocked = RunLifecycle.BLOCKED
        engine, adapter, runs, bus, _ref = build()
        artifact = load_artifact("two-rules-one-event.artifact.json")
        mutated = with_step(
            artifact,
            "review-done",
            artifact.steps["review-done"].model_copy(update={"outcome": "blocked"}),
        )
        engine.services.artifact_store.put(mutated)
        adapter.queue.append(ok())

        outcome = await engine.run_rule(
            artifact_ref_for(mutated), "review", event("task-completed-code"), TRUSTED_LOCAL
        )

        assert outcome.lifecycle is blocked
        assert outcome.outcome == "blocked"
        finished = [payload for name, payload in bus.events if name == "playbook.v2.run.finished"]
        assert len(finished) == 1
        assert finished[0]["run_id"] == outcome.run_id
        assert finished[0]["lifecycle"] == "blocked"
        assert finished[0]["outcome"] == "blocked"
        terminal_receipt = next(
            receipt
            for receipt in runs.receipts
            if receipt.step_id == "review-done" and receipt.receipt_kind == "step"
        )
        assert terminal_receipt.outcome == "failure"


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
        # A shadow run's record is its symbolic traversal, held in memory:
        # every command it would have issued, and every edge it forked over.
        assert outcome.traversal is not None and outcome.traversal.paths
        assert outcome.commands and outcome.commands[0][1] == "ensure_task"
        assert outcome.lifecycle is RunLifecycle.COMPLETED


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
        assert [r.step_id for r in runs.receipts if r.receipt_kind == "step"] == [
            "ensure-review-task",
            "review-done",
        ]


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
        retired = [w for w in self.active.values() if w.run_id == run_id]
        for wait in retired:
            self.active.pop(wait.wait_id, None)
            self.cleared.append(wait.wait_id)
        return len(retired)

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
        if wait_changes.clear_run_waits:
            await self.waits.clear_for_run(snapshot.run_id)
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
        # Shadow reports the wait as an unresolved boundary and forks over
        # its declared outcomes instead of pausing a cursor on it (§4.10).
        assert outcome.lifecycle is RunLifecycle.COMPLETED
        wait_nodes = [
            node
            for path in outcome.traversal.paths
            for node in path.nodes
            if node.status == "unresolved" and node.reason == UNRESOLVED_REASON
        ]
        assert wait_nodes
        assert all(path.completed is False for path in outcome.traversal.paths)

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
    async def test_live_loop_allows_the_declared_default_max_iterations(self):
        engine, adapter, _runs, ref = loop_engine()
        ids = [f"d-{index}" for index in range(500)]
        adapter.queue.extend([downstream(*ids), *(ok() for _ in ids)])

        outcome = await engine.run_rule(ref, "sweep", event("spec-approved"), TRUSTED_LOCAL)

        assert outcome.lifecycle is RunLifecycle.COMPLETED
        assert outcome.outcome == "completed"
        assert len(adapter.args_for("ensure_task")) == 500
        assert outcome.snapshot.bindings["sweep_result"]["total"] == 500

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
        # Every boundary identity is distinct, which is what the database's
        # uq_playbook_step_receipts_boundary enforces independently.
        keys = [
            (r.step_id, r.iteration, r.attempt, r.turn_index, r.receipt_kind)
            for r in runs.receipts
        ]
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


# --------------------------------------------------------------------------
# T-9 — cancellation is real (child plan §4.9)
#
# §5.2 names these tests in ``tests/test_v2_cancellation.py``; per §2.6 item
# 12 the roadmap's Package 4 file list is the scope authority and creates no
# such file, so the names are kept verbatim and live here.
# --------------------------------------------------------------------------


class HeldExecutor:
    """A live command executor a test can hold inside ``execute``.

    The engine's cancellation path is only reachable while a step is
    genuinely in flight, and "in flight" is not something a scripted return
    value can express — so the double blocks on an event the test owns.
    """

    step_type = "command"
    mode = ExecutionMode.LIVE
    no_side_effects = False

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.calls = 0

    async def execute(self, step, ctx):
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        return ExecutorResult(
            control=StepControl.ADVANCE,
            outcome="created",
            value={"task_id": "t-1", "created": True},
            receipt_result={"task_id": "t-1", "created": True},
        )


class CancellableExecutor(HeldExecutor):
    """``HeldExecutor`` that also implements the optional protocol (§3.1)."""

    def __init__(self, *, acknowledge: bool = True) -> None:
        super().__init__()
        self.acknowledge = acknowledge
        self.cancels = 0

    async def request_cancel(self, step, ctx) -> None:
        self.cancels += 1
        if self.acknowledge:
            self.gate.set()


def held_executor_table(executor):
    """``executor_for`` with *executor* substituted for live commands."""
    real = executor_for

    def _for(step_type: str, mode: ExecutionMode):
        if step_type == "command" and ExecutionMode(mode) is ExecutionMode.LIVE:
            return executor
        return real(step_type, mode)

    return _for


async def start_held_run(engine, ref, runs, executor, monkeypatch, rule: str = "review"):
    """Walk *rule* until its command is inside ``execute``, and hand it back."""
    monkeypatch.setattr(
        "src.playbooks.engine.executor_for", held_executor_table(executor)
    )
    walk = asyncio.create_task(
        engine.run_rule(ref, rule, event("task-completed-code"), TRUSTED_LOCAL)
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=2)
    run_id = next(iter(runs.snapshots))
    return walk, run_id


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_a_paused_run_is_immediate(self):
        """§4.9's paused row: one boundary, one receipt, wait deregistered."""
        engine, runs, waits, ref = wait_engine()
        outcome = await engine.run_rule(ref, "gate", event("task-completed-code"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.PAUSED
        assert list(waits.active)

        before = runs.commit_calls
        cancelled = await engine.cancel(outcome.run_id, TRUSTED_LOCAL, reason="operator")

        assert cancelled.lifecycle is RunLifecycle.CANCELLED
        assert runs.commit_calls == before + 1
        assert runs.snapshots[outcome.run_id].lifecycle is RunLifecycle.CANCELLED
        # The wait went with the run, in that same boundary: a cancelled run
        # whose wait is still active is claimable by a later event.
        assert runs.wait_changes[-1].clear_run_waits is True
        assert waits.active == {}
        receipt = runs.receipts[-1]
        assert receipt.outcome == "cancelled"
        assert receipt.cancelled_at is not None
        assert receipt.result["cancellation"] == "acknowledged"

    @pytest.mark.asyncio
    async def test_cancel_during_a_live_command_does_not_get_overwritten(
        self, monkeypatch
    ):
        """The regression test for ``playbook_commands.py:511-519``'s docstring.

        V1 says of itself that a live run "will finish its current node and
        then, on its next persistence write, silently overwrite the
        ``cancelled`` status back to ``running``".  Here the command is held
        mid-flight, the run is cancelled, and the command is then released:
        the run must still be ``cancelled``, and the step's own result must
        not have routed an edge.
        """
        engine, _adapter, runs, _bus, ref = build()
        executor = HeldExecutor()
        walk, run_id = await start_held_run(engine, ref, runs, executor, monkeypatch)

        cancelling = asyncio.create_task(engine.cancel(run_id, TRUSTED_LOCAL))
        await asyncio.sleep(0)
        executor.gate.set()

        cancelled = await asyncio.wait_for(cancelling, timeout=2)
        outcome = await asyncio.wait_for(walk, timeout=2)

        assert cancelled.lifecycle is RunLifecycle.CANCELLED
        assert outcome.lifecycle is RunLifecycle.CANCELLED
        assert runs.snapshots[run_id].lifecycle is RunLifecycle.CANCELLED
        # Not ``completed``, and no transition was selected off the step that
        # was in flight — the run stopped, it did not finish.
        assert [r.outcome for r in runs.receipts] == ["started", "cancelled"]
        assert runs.receipts[-1].selected_transition is None

    @pytest.mark.asyncio
    async def test_in_flight_executor_gets_one_cancel_signal(self, monkeypatch):
        """§4.9 — at most one ``request_cancel`` per in-flight step."""
        engine, _adapter, runs, _bus, ref = build()
        executor = CancellableExecutor(acknowledge=True)
        walk, run_id = await start_held_run(engine, ref, runs, executor, monkeypatch)

        first, second = await asyncio.wait_for(
            asyncio.gather(
                engine.cancel(run_id, TRUSTED_LOCAL),
                engine.cancel(run_id, TRUSTED_LOCAL),
            ),
            timeout=2,
        )
        await asyncio.wait_for(walk, timeout=2)

        assert executor.cancels == 1
        assert first.lifecycle is RunLifecycle.CANCELLED
        assert second.lifecycle is RunLifecycle.CANCELLED
        assert runs.receipts[-1].result["cancellation"] == "acknowledged"
        # Two callers, one boundary: cancelling twice does not receipt twice.
        assert [r.outcome for r in runs.receipts] == ["started", "cancelled"]

    @pytest.mark.asyncio
    async def test_grace_expiry_still_reaches_cancelled(self, monkeypatch):
        """The executor never gives the run back; the run ends anyway."""
        engine, _adapter, runs, _bus, ref = build()
        engine.cancellation_grace_seconds = 0.05
        executor = CancellableExecutor(acknowledge=False)
        walk, run_id = await start_held_run(engine, ref, runs, executor, monkeypatch)

        cancelled = await asyncio.wait_for(
            engine.cancel(run_id, TRUSTED_LOCAL), timeout=2
        )

        assert executor.cancels == 1
        assert cancelled.lifecycle is RunLifecycle.CANCELLED
        assert cancelled.cancellation == "grace_expired"
        assert runs.snapshots[run_id].lifecycle is RunLifecycle.CANCELLED
        assert runs.receipts[-1].result["cancellation"] == "grace_expired"

        # The step is still running — the engine ends runs, it does not kill
        # work it did not start — and when it finally returns it writes
        # nothing, because the run is already settled.
        executor.gate.set()
        outcome = await asyncio.wait_for(walk, timeout=2)
        assert outcome.lifecycle is RunLifecycle.CANCELLED
        assert [r.outcome for r in runs.receipts] == ["started", "cancelled"]

    @pytest.mark.asyncio
    async def test_cancel_a_terminal_run_is_refused(self):
        """Same sentence as ``playbook_commands.py:544``."""
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        outcome = await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)
        assert outcome.lifecycle is RunLifecycle.COMPLETED

        before = runs.commit_calls
        refused = await engine.cancel(outcome.run_id, TRUSTED_LOCAL)

        assert refused.outcome == "already_terminal"
        assert refused.error == f"Run '{outcome.run_id}' already completed"
        assert runs.commit_calls == before
        assert runs.cancel_reasons == []

    @pytest.mark.asyncio
    async def test_cancelling_an_unknown_run_is_refused(self):
        engine, _adapter, _runs, _bus, _ref = build()
        outcome = await engine.cancel("no-such-run", TRUSTED_LOCAL)
        assert outcome.outcome == "unknown_run"

    @pytest.mark.asyncio
    async def test_a_run_cancelled_between_walks_stops_at_its_next_boundary(self):
        """§4.9's "running, nothing in flight" row, across a restart.

        The intent is durable and the walk that reads it is a *different* one,
        which is the property that makes cancellation survive the process
        that was asked for it going away.
        """
        engine, adapter, runs, _bus, ref = build()
        adapter.queue.append(ok())
        outcome = await engine.run_rule(
            ref, "review", event("task-completed-code"), TRUSTED_LOCAL, pause_before_start=True
        )
        stored = runs.snapshots[outcome.run_id]
        runs.snapshots[outcome.run_id] = replace(stored, lifecycle=RunLifecycle.RUNNING)

        cancelled = await engine.cancel(outcome.run_id, TRUSTED_LOCAL, reason="operator")
        assert cancelled.lifecycle is RunLifecycle.CANCELLING
        assert cancelled.outcome == "cancel_requested"
        assert runs.cancel_reasons[-1][1] == "operator"

        resumed = await engine.resume(
            outcome.run_id, EventArrived(event_id="x", payload={}), TRUSTED_LOCAL
        )
        assert resumed.lifecycle is RunLifecycle.CANCELLED
        assert [r.outcome for r in runs.receipts] == ["cancelled"]

