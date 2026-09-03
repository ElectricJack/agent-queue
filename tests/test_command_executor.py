"""The V2 command executor — Package 4 child plan T-2 and §3.2.

The six ``_consume`` checks are ordered and each has its own failure mode; a
reordering is a behaviour change, so they are asserted individually rather
than through one happy path.  The two assertions that matter most are the
direct replacements for ``pipeline_runner.py:145``, which inferred success
from the *shape* of a handler dict: a command returning ``{}`` read as a
success and one returning ``{"error": None}`` read as a failure.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.definition import CommandStep
from src.playbooks.executors import executor_for
from src.playbooks.executors.base import (
    EngineServices,
    ExecutionMode,
    StepContext,
    StepControl,
)
from src.playbooks.expressions import ResolutionScope
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
) -> StepContext:
    artifact = minimal_artifact()
    return StepContext(
        run_id="run-1",
        dispatch_id="d-1",
        artifact_ref=artifact_ref_for(artifact),
        artifact=artifact,
        rule_id="r",
        step_id=step_id,
        principal=TRUSTED_LOCAL,
        scope=ResolutionScope(),
        services=EngineServices(contracts=registry, clock=lambda: 100.0),
        mode=mode,
        attempt=attempt,
        iteration_index=iteration_index,
        inputs=inputs if inputs is not None else {"project_id": "p", "title": "Review"},
    )


async def run(step: CommandStep, ctx: StepContext, mode: ExecutionMode = ExecutionMode.LIVE):
    return await executor_for(step.type, mode).execute(step, ctx)


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
    @pytest.mark.asyncio
    async def test_keyed_command_receives_the_attempt_key(self):
        registry, adapter = registry_with(ENSURE_TASK)
        adapter.queue.append(
            CommandResult(
                outcome="created", value=EnsureTaskResult(task_id="t", created=True), summary=""
            )
        )
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
            if "CommandResult" in text and path.name != "command.py":
                offenders.append(str(path))
        assert offenders == []
