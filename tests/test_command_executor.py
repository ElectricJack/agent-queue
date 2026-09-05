"""The V2 command executor — Package 4 child plan T-2 and §3.2.

The six ``_consume`` checks are ordered and each has its own failure mode; a
reordering is a behaviour change, so they are asserted individually rather
than through one happy path.  The two assertions that matter most are the
direct replacements for ``pipeline_runner.py:145``, which inferred success
from the *shape* of a handler dict: a command returning ``{}`` read as a
success and one returning ``{"error": None}`` read as a failure.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.contracts.registry import CommandRegistration, ContractRegistry
from src.commands.principal import ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL
from src.profiles.capabilities import DENY_ALL
from src.playbooks.definition import CommandStep
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    StepContext,
    StepControl,
)
from src.playbooks.expressions import ResolutionScope
from src.playbooks.invocation import current_invocation
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    GATE_CREATE,
    LIST_TASKS,
    NO_PROJECTION,
    TWO_FAILURES,
    EnsureTaskResult,
    GateCreateResult,
    ListTasksResult,
    NoProjectionResult,
    TwoFailuresResult,
    registry_with,
)
from tests.playbook_v2_engine_helpers import artifact_ref_for, minimal_artifact


def command_step(**overrides: Any) -> CommandStep:
    payload: dict[str, Any] = {
        "rule": "r",
        "title": "Ensure",
        "source": {"path": "x.md", "start_line": 1, "end_line": 1},
        "command": "ensure_task",
        "inputs": {},
        "save_result_as": "review",
        "transitions": {"created": "done", "reused": "done", "rejected": "bad"},
    }
    payload.update(overrides)
    return CommandStep.model_validate(payload)


def context(
    registry: Any,
    *,
    inputs: dict[str, Any] | None = None,
    mode: ExecutionMode = ExecutionMode.LIVE,
    step_id: str = "ensure-review-task",
    attempt: int = 1,
    iteration_index: int | None = None,
    authored_idempotency_key: str | None = None,
    run_id: str = "run-1",
    dispatch_id: str = "d-1",
    principal: ExecutionPrincipal = TRUSTED_LOCAL,
) -> StepContext:
    artifact = minimal_artifact()
    return StepContext(
        run_id=run_id,
        dispatch_id=dispatch_id,
        artifact_ref=artifact_ref_for(artifact),
        artifact=artifact,
        rule_id="r",
        step_id=step_id,
        principal=principal,
        scope=ResolutionScope(),
        services=EngineServices(contracts=registry, clock=lambda: 100.0),
        mode=mode,
        attempt=attempt,
        iteration_index=iteration_index,
        inputs=inputs if inputs is not None else {"project_id": "p", "title": "Review"},
        authored_idempotency_key=authored_idempotency_key,
    )


async def run(step: CommandStep, ctx: StepContext, mode: ExecutionMode = ExecutionMode.LIVE):
    return await executor_for(step.type, mode).execute(step, ctx)


class TestLiveInvocationContext:
    @pytest.mark.asyncio
    async def test_adapter_observes_current_step_not_delegation_ancestry(self):
        observed = []

        async def invoke(args, principal):
            observed.append(current_invocation())
            return CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t", created=True),
                summary="",
            )

        registry = ContractRegistry()
        registry.register(
            CommandRegistration(
                name="ensure_task", contract=ENSURE_TASK, invoke=invoke
            )
        )
        principal = ExecutionPrincipal(
            kind=PrincipalKind.PLAYBOOK,
            policy=DENY_ALL,
            parent_run_id="delegating-run",
            parent_step_id="delegating-step",
        )
        ctx = context(
            registry,
            run_id="current-run",
            dispatch_id="current-dispatch",
            step_id="current-step",
            attempt=4,
            principal=principal,
        )

        assert current_invocation() is None
        result = await run(command_step(), ctx)

        assert result.outcome == "created"
        invocation = observed[0]
        assert (
            invocation.run_id,
            invocation.dispatch_id,
            invocation.rule_id,
            invocation.step_id,
            invocation.attempt,
        ) == ("current-run", "current-dispatch", "r", "current-step", 4)
        assert invocation.artifact_ref == ctx.artifact_ref
        assert invocation.run_id != principal.parent_run_id
        assert current_invocation() is None

    @pytest.mark.asyncio
    async def test_nested_live_dispatch_restores_outer_invocation(self):
        observed = []
        registry = ContractRegistry()

        async def invoke_inner(args, principal):
            observed.append(("inner", current_invocation()))
            return CommandResult(
                outcome="listed", value=ListTasksResult(count=0), summary=""
            )

        async def invoke_outer(args, principal):
            observed.append(("outer-before", current_invocation()))
            inner_step = command_step(
                command="list_tasks",
                save_result_as="tasks",
                transitions={"listed": "done"},
            )
            inner = await run(
                inner_step,
                context(
                    registry,
                    inputs={"project_id": "p"},
                    run_id="inner-run",
                    dispatch_id="inner-dispatch",
                    step_id="inner-step",
                ),
            )
            assert inner.outcome == "listed"
            observed.append(("outer-after", current_invocation()))
            return CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t", created=True),
                summary="",
            )

        registry.register(
            CommandRegistration(
                name="ensure_task", contract=ENSURE_TASK, invoke=invoke_outer
            )
        )
        registry.register(
            CommandRegistration(
                name="list_tasks",
                contract=LIST_TASKS,
                invoke=invoke_inner,
                preview=invoke_inner,
            )
        )

        result = await run(
            command_step(),
            context(
                registry,
                run_id="outer-run",
                dispatch_id="outer-dispatch",
                step_id="outer-step",
            ),
        )

        assert result.outcome == "created"
        assert [(label, value.run_id) for label, value in observed] == [
            ("outer-before", "outer-run"),
            ("inner", "inner-run"),
            ("outer-after", "outer-run"),
        ]
        assert current_invocation() is None

    @pytest.mark.asyncio
    async def test_adapter_exception_restores_empty_invocation(self):
        async def invoke(args, principal):
            assert current_invocation().run_id == "exception-run"
            raise RuntimeError("boom")

        registry = ContractRegistry()
        registry.register(
            CommandRegistration(
                name="ensure_task", contract=ENSURE_TASK, invoke=invoke
            )
        )

        result = await run(
            command_step(), context(registry, run_id="exception-run")
        )

        assert result.outcome == "runtime_error"
        assert current_invocation() is None

    @pytest.mark.asyncio
    async def test_adapter_cancellation_restores_empty_invocation(self):
        async def invoke(args, principal):
            assert current_invocation().run_id == "cancelled-run"
            raise asyncio.CancelledError

        registry = ContractRegistry()
        registry.register(
            CommandRegistration(
                name="ensure_task", contract=ENSURE_TASK, invoke=invoke
            )
        )

        with pytest.raises(asyncio.CancelledError):
            await run(command_step(), context(registry, run_id="cancelled-run"))
        assert current_invocation() is None

    @pytest.mark.asyncio
    async def test_promotion_provenance_uses_current_invocation_not_ancestry(self):
        from types import SimpleNamespace

        from src.integration.promotion import PromotionService

        class ProvenanceDB:
            async def get_task(self, task_id):
                assert task_id == "source-task"
                return SimpleNamespace(branch_name="aq/source-task")

            async def get_integration_operation_artifact_route(self, operation_id):
                assert operation_id == "operation-1"
                ref = artifact_ref_for(minimal_artifact())
                return {
                    "playbook_id": ref.playbook_id,
                    "artifact_sha256": ref.artifact_sha256,
                    "artifact_snapshot": ref.as_dict(),
                }

        service = PromotionService(ProvenanceDB(), data_dir=".")
        observed = {}

        async def invoke(args, principal):
            observed.update(
                await service._provenance(
                    {
                        "source_task_id": "source-task",
                        "reviewer_session_attempt_id": None,
                    },
                    operation_id="operation-1",
                )
            )
            return CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t", created=True),
                summary="",
            )

        registry = ContractRegistry()
        registry.register(
            CommandRegistration(
                name="ensure_task", contract=ENSURE_TASK, invoke=invoke
            )
        )
        principal = ExecutionPrincipal(
            kind=PrincipalKind.PLAYBOOK,
            policy=DENY_ALL,
            parent_run_id="ancestor-run",
            parent_step_id="ancestor-step",
        )

        await run(
            command_step(),
            context(
                registry,
                run_id="promotion-run",
                step_id="promotion-step",
                attempt=7,
                principal=principal,
            ),
        )

        assert observed["playbook_run_id"] == "promotion-run"
        assert observed["playbook_step_id"] == "promotion-step"
        assert observed["playbook_attempt"] == 7
        assert observed["playbook_run_id"] != principal.parent_run_id

    @pytest.mark.asyncio
    async def test_promotion_rejects_invocation_artifact_outside_frozen_operation(self):
        from types import SimpleNamespace

        from src.integration.promotion import PromotionService

        class ProvenanceDB:
            async def get_task(self, task_id):
                return SimpleNamespace(branch_name="aq/source-task")

            async def get_integration_operation_artifact_route(self, operation_id):
                ref = artifact_ref_for(minimal_artifact()).as_dict()
                ref["artifact_sha256"] = "sha256:" + "f" * 64
                return {
                    "playbook_id": ref["playbook_id"],
                    "artifact_sha256": ref["artifact_sha256"],
                    "artifact_snapshot": ref,
                }

        service = PromotionService(ProvenanceDB(), data_dir=".")

        async def invoke(args, principal):
            await service._provenance(
                {
                    "source_task_id": "source-task",
                    "reviewer_session_attempt_id": None,
                },
                operation_id="operation-1",
            )
            return CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t", created=True),
                summary="",
            )

        registry = ContractRegistry()
        registry.register(
            CommandRegistration(
                name="ensure_task", contract=ENSURE_TASK, invoke=invoke
            )
        )

        result = await run(command_step(), context(registry))

        assert result.outcome == "runtime_error"
        assert result.diagnostics == ("PromotionInvariantError",)


class TestConsumeOrder:
    """§3.2's six checks, each with its own failure mode."""

    @pytest.mark.asyncio
    async def test_bare_dict_result_is_a_contract_violation(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append({"task_id": "t-1", "created": True})
        result = await run(command_step(), context(registry))
        assert result.outcome == "contract_violation"
        assert result.value is None

    @pytest.mark.asyncio
    async def test_undeclared_outcome_is_a_contract_violation(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(outcome="weird", value=EnsureTaskResult(task_id="t", created=True), summary="")
        )
        result = await run(command_step(), context(registry))
        assert result.outcome == "contract_violation"

    @pytest.mark.asyncio
    async def test_result_of_the_wrong_model_is_a_contract_violation(self):
        registry, adapter = registry_with(ENSURE_TASK, LIST_TASKS)
        adapter.queue.append(
            CommandResult(outcome="created", value=ListTasksResult(count=0), summary="")
        )
        result = await run(command_step(), context(registry))
        assert result.outcome == "contract_violation"

    @pytest.mark.asyncio
    async def test_a_business_outcome_with_no_edge_is_a_contract_violation(self):
        """§3.2 step 4 — re-checked at run time because an artifact can be
        pinned across a contract change."""
        registry, adapter = registry_with(TWO_FAILURES)
        adapter.queue.append(
            CommandResult(outcome="conflict", value=TwoFailuresResult(), summary="")
        )
        step = command_step(
            command="two_failures",
            save_result_as=None,
            transitions={"ok": "done"},
        )
        result = await run(step, context(registry, inputs={"project_id": "p"}))
        assert result.outcome == "contract_violation"

    @pytest.mark.asyncio
    async def test_the_bound_value_is_the_declared_model_not_a_handler_dict(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t-1", created=True), summary="ok"
            )
        )
        result = await run(command_step(), context(registry))
        assert result.outcome == "created"
        assert result.value == {"task_id": "t-1", "created": True}

    @pytest.mark.asyncio
    async def test_no_save_result_as_binds_nothing(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t-1", created=True), summary="ok"
            )
        )
        result = await run(command_step(save_result_as=None), context(registry))
        assert result.value is None

    @pytest.mark.asyncio
    async def test_result_over_256_kib_is_state_limit_exceeded(self):
        registry, adapter = registry_with(GATE_CREATE)
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=GateCreateResult(gate_id="g" * 300_000),
                summary="",
            )
        )
        step = command_step(
            command="gate_create",
            save_result_as="gate",
            transitions={"created": "done", "reused": "done", "skipped": "done", "rejected": "bad"},
        )
        result = await run(step, context(registry, inputs={"task_id": "t", "title": "T"}))
        assert result.outcome == "state_limit_exceeded"
        assert result.value is None


class TestRoutingIsByOutcomeNotClassification:
    @pytest.mark.asyncio
    async def test_two_failure_outcomes_take_different_edges(self):
        """The direct replacement for ``pipeline_runner.py:145``.

        Both outcomes classify as FAILURE.  Nothing downstream may collapse
        them, so each must carry its own name to the engine's transition
        lookup.
        """
        registry, adapter = registry_with(TWO_FAILURES)
        step = command_step(
            command="two_failures",
            save_result_as=None,
            transitions={
                "ok": "done",
                "not_found": "no-tasks",
                "conflict": "retry-later",
                "runtime_error": "failed-end",
            },
        )
        outcomes = []
        for name in ("not_found", "conflict"):
            adapter.queue.append(
                CommandResult(outcome=name, value=TwoFailuresResult(detail=name), summary="")
            )
            result = await run(step, context(registry, inputs={"project_id": "p"}))
            outcomes.append(result.outcome)
        assert outcomes == ["not_found", "conflict"]
        assert step.transitions[outcomes[0]] != step.transitions[outcomes[1]]

    @pytest.mark.asyncio
    async def test_empty_result_is_not_treated_as_success(self):
        """``{}`` was a success under the V1 shape check.  Here the outcome
        name decides, and an empty *model* on a failure outcome is a failure."""
        registry, adapter = registry_with(TWO_FAILURES)
        adapter.queue.append(
            CommandResult(outcome="not_found", value=TwoFailuresResult(), summary="")
        )
        step = command_step(
            command="two_failures",
            save_result_as="probe",
            transitions={"ok": "done", "not_found": "no-tasks", "conflict": "c", "runtime_error": "e"},
        )
        result = await run(step, context(registry, inputs={"project_id": "p"}))
        assert result.outcome == "not_found"
        assert result.control is StepControl.ADVANCE


class TestIdempotencyKey:
    """Three sources for a keyed contract's key argument, and the engine's is
    last.  The precedence is the whole point: a keyed field is a *semantic*
    identity (``ensure_task``'s ``dedup_key``, ``task_batch_commit``'s
    ``proposal_id``), so overwriting an authored one with the attempt key
    turns "create or reuse by this key" into "create every time"."""

    @pytest.mark.asyncio
    async def test_authored_key_field_survives_the_attempt_key(self):
        """The regression this class exists for.

        ``per-task-review`` authors ``dedup_key`` ``review:task:<task_id>`` so
        that two different ``task.completed`` events for one task converge on
        a single review task.  The attempt key is per-dispatch, so injecting it
        would mint a new review task per event — and ``src/doctor``'s
        stranded-PR check, which looks the row up by that exact key, would
        never find one.
        """
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t", created=True), summary=""
            )
        )
        ctx = context(
            registry,
            inputs={"project_id": "p", "title": "Review", "dedup_key": "review:task:TASK-1"},
            attempt=3,
        )
        result = await run(command_step(), ctx)
        args = adapter.args_for("ensure_task")[0]
        assert args.dedup_key == "review:task:TASK-1"
        # The attempt key is not lost — it is on the receipt, where attempt
        # identity belongs, and it is *not* the argument.
        assert result.idempotency_key == "run-1:ensure-review-task:-:3"

    @pytest.mark.asyncio
    async def test_authored_key_field_that_resolved_to_none_stays_none(self):
        """Presence wins over value.

        A key the author bound and that resolved to ``None`` is an authoring
        problem for the argument model to report; substituting an attempt key
        would invent an identity nobody asked for and hide the mistake behind
        a successful create.
        """
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t", created=True), summary=""
            )
        )
        ctx = context(
            registry, inputs={"project_id": "p", "title": "Review", "dedup_key": None}
        )
        await run(command_step(), ctx)
        assert adapter.args_for("ensure_task")[0].dedup_key is None

    @pytest.mark.asyncio
    async def test_step_level_override_beats_both(self):
        """``CommandStep.idempotency_key`` is documented as overriding the
        contract default and the graph projection renders it as "keyed by this
        step"; it is resolved by the engine and applied here."""
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t", created=True), summary=""
            )
        )
        ctx = context(
            registry,
            inputs={"project_id": "p", "title": "Review", "dedup_key": "review:task:TASK-1"},
            authored_idempotency_key="review-of-TASK-1",
        )
        result = await run(command_step(), ctx)
        assert adapter.args_for("ensure_task")[0].dedup_key == "review-of-TASK-1"
        assert result.idempotency_key == "run-1:ensure-review-task:-:1"

    @pytest.mark.asyncio
    async def test_keyed_command_receives_the_attempt_key(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t", created=True), summary=""
            )
        )
        # No authored key anywhere: the engine still guarantees a keyed
        # contract gets a key, so the fallback branch is preserved.
        result = await run(command_step(), context(registry, iteration_index=2, attempt=3))
        args = adapter.args_for("ensure_task")[0]
        assert args.dedup_key == "run-1:ensure-review-task:2:3"
        assert result.idempotency_key == args.dedup_key

    @pytest.mark.asyncio
    async def test_unkeyed_command_receives_no_extra_argument(self):
        registry, adapter = registry_with(LIST_TASKS)
        adapter.queue.append(
            CommandResult(outcome="listed", value=ListTasksResult(count=0), summary="")
        )
        step = command_step(
            command="list_tasks", save_result_as="downstream", transitions={"listed": "done"}
        )
        result = await run(step, context(registry, inputs={"project_id": "p"}))
        args = adapter.args_for("list_tasks")[0]
        assert set(args.model_dump(exclude_none=True)) == {"project_id"}
        # Recorded on the receipt even when it is not passed (§3.3.2).
        assert result.idempotency_key == "run-1:ensure-review-task:-:1"


class TestBoundaryRevalidation:
    @pytest.mark.asyncio
    async def test_runtime_arguments_are_revalidated_at_the_boundary(self):
        """The compiler validated the *types of the references*; only the
        boundary can see the resolved values."""
        registry, adapter = registry_with(ENSURE_TASK)
        result = await run(
            command_step(), context(registry, inputs={"project_id": 17, "title": "Review"})
        )
        assert result.outcome == "input_resolution_failed"
        assert adapter.calls == []

    @pytest.mark.asyncio
    async def test_an_undeclared_argument_is_rejected_before_invocation(self):
        registry, adapter = registry_with(ENSURE_TASK)
        result = await run(
            command_step(),
            context(registry, inputs={"project_id": "p", "title": "R", "smuggled": "x"}),
        )
        assert result.outcome == "input_resolution_failed"
        assert adapter.calls == []

    @pytest.mark.asyncio
    async def test_uncontracted_command_cannot_execute(self):
        registry, adapter = registry_with(ENSURE_TASK)
        result = await run(command_step(command="not_contracted"), context(registry))
        assert result.outcome == "contract_violation"
        assert adapter.calls == []

    @pytest.mark.asyncio
    async def test_an_unexpected_adapter_exception_is_a_runtime_error(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(RuntimeError("boom with a secret argument in it"))
        result = await run(command_step(), context(registry))
        assert result.outcome == "runtime_error"
        # The type, never the message — a message can carry an argument value.
        assert result.diagnostics == ("RuntimeError",)


class TestReceiptProjection:
    @pytest.mark.asyncio
    async def test_only_projected_result_fields_reach_the_receipt(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t-1", created=True), summary=""
            )
        )
        result = await run(command_step(), context(registry))
        assert set(result.receipt_result) <= set(ENSURE_TASK.execution.receipt_projection)

    @pytest.mark.asyncio
    async def test_empty_projection_projects_nothing(self):
        registry, adapter = registry_with(NO_PROJECTION)
        adapter.queue.append(
            CommandResult(outcome="done", value=NoProjectionResult(), summary="")
        )
        step = command_step(
            command="no_projection", save_result_as="np", transitions={"done": "done"}
        )
        result = await run(step, context(registry, inputs={"project_id": "p"}))
        assert "populated" not in result.receipt_result

    @pytest.mark.asyncio
    async def test_a_sensitive_projected_field_is_hashed_not_shown(self):
        registry, adapter = registry_with(GATE_CREATE)
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=GateCreateResult(gate_id="g-1", secret_note="do not print"),
                summary="",
            )
        )
        step = command_step(
            command="gate_create",
            save_result_as="gate",
            transitions={"created": "done", "reused": "done", "skipped": "done", "rejected": "bad"},
        )
        result = await run(step, context(registry, inputs={"task_id": "t", "title": "T"}))
        assert result.receipt_result["gate_id"] == "g-1"
        assert result.receipt_result["secret_note"].startswith("sensitive:")
        assert "do not print" not in str(result.receipt_result)


class TestModes:
    @pytest.mark.asyncio
    async def test_shadow_never_invokes_anything_not_even_a_preview(self):
        registry, adapter = registry_with(LIST_TASKS)
        step = command_step(
            command="list_tasks", save_result_as="downstream", transitions={"listed": "done"}
        )
        ctx = context(registry, inputs={"project_id": "p"}, mode=ExecutionMode.SHADOW)
        result = await run(step, ctx, ExecutionMode.SHADOW)
        assert result.control is StepControl.UNRESOLVED
        assert adapter.calls == [] and adapter.preview_calls == []
        assert result.possible_outcomes == ("listed",)

    @pytest.mark.asyncio
    async def test_dry_run_uses_the_preview_adapter_when_the_contract_has_one(self):
        registry, adapter = registry_with(LIST_TASKS)
        adapter.preview_queue.append(
            CommandResult(outcome="listed", value=ListTasksResult(count=2), summary="preview")
        )
        step = command_step(
            command="list_tasks", save_result_as="downstream", transitions={"listed": "done"}
        )
        ctx = context(registry, inputs={"project_id": "p"}, mode=ExecutionMode.DRY_RUN)
        result = await run(step, ctx, ExecutionMode.DRY_RUN)
        assert result.outcome == "listed"
        assert adapter.calls == []
        assert len(adapter.preview_calls) == 1

    @pytest.mark.asyncio
    async def test_dry_run_without_a_preview_adapter_is_unresolved(self):
        registry, adapter = registry_with(ENSURE_TASK)
        ctx = context(registry, mode=ExecutionMode.DRY_RUN)
        result = await run(command_step(), ctx, ExecutionMode.DRY_RUN)
        assert result.control is StepControl.UNRESOLVED
        assert result.diagnostics == ("no preview adapter",)
        assert adapter.calls == [] and adapter.preview_calls == []

    @pytest.mark.asyncio
    async def test_every_non_live_command_executor_declares_no_side_effects(self):
        for mode in (ExecutionMode.DRY_RUN, ExecutionMode.SHADOW):
            assert executor_for("command", mode).no_side_effects is True
        assert executor_for("command", ExecutionMode.LIVE).no_side_effects is False


class TestAdapterProtocol:
    @pytest.mark.asyncio
    async def test_a_reserved_outcome_from_the_adapter_passes_through_unchanged(self):
        """§3.2's reserved-outcome mapping: the engine never rewrites one."""
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="unauthorized",
                value=EnsureTaskResult(task_id="", created=False),
                summary="denied",
            )
        )
        step = command_step(transitions={"created": "done", "reused": "done", "rejected": "bad",
                                         "runtime_error": "bad"})
        result = await run(step, context(registry))
        assert result.outcome == "unauthorized"

    def test_the_scripted_adapter_is_the_only_path_to_a_handler(self):
        """There is no second consumer of a raw handler result in the
        playbooks package — §3.2's "one function, in one file"."""
        import pathlib

        offenders = []
        for path in pathlib.Path("src/playbooks").rglob("*.py"):
            if path.name in {"runner.py", "pipeline_runner.py", "runner_context.py",
                             "runner_transitions.py", "runner_events.py"}:
                continue  # V1, deleted by Package 7
            text = path.read_text()
            # ``agent_task.py`` is the second — and, by design, last —
            # dispatcher of a contracted command: it creates the child task
            # through ``create_task`` (T-8, §4.5 step 2).  It reads exactly
            # the declared ``outcome`` and ``value.task_id`` off the typed
            # result and never infers success from a dict's shape, which is
            # the property this test exists to protect.
            if "CommandResult" in text and path.name not in {"command.py", "agent_task.py"}:
                offenders.append(str(path))
        assert offenders == []
