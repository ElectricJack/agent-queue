"""Receipt completeness and default-deny redaction — Package 4 T-4.

Package 5 projects these receipts into the operator overlay and Packages 6
and 7 measure them, so "what is on a receipt" is an interface, not an
implementation detail.  The redaction half matters more than the
completeness half: the design spec's rule is *default-deny*, so a field that
nobody classified must be absent rather than masked — a masked key still
leaks that the field was populated.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.commands.contracts.models import CommandResult
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.receipts import REDACTED_KEY, SENSITIVE_PREFIX
from tests.fixtures.contracts.engine_contracts import (
    ENSURE_TASK,
    GATE_CREATE,
    LIST_TASKS,
    NO_PROJECTION,
    EnsureTaskResult,
    GateCreateResult,
    NoProjectionResult,
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

#: §3.3.3's mandatory rows, mapped onto Package 3's column names (§2.5 item 2).
MANDATORY_FIELDS = (
    "run_id",
    "rule_id",
    "step_id",
    "step_kind",
    "artifact_sha256",
    "attempt",
    "outcome",
    "idempotency_key",
    "started_at",
    "completed_at",
    "snapshot_version",
)


#: One resolvable ``inputs`` block per contract double.  Swapping the command
#: without swapping its arguments would fail at the boundary revalidation
#: rather than at the projection this suite is about.
INPUTS: dict[str, dict[str, Any]] = {
    "ensure_task": {
        "project_id": {"type": "event_ref", "path": "project_id"},
        "title": {"type": "event_ref", "path": "title"},
    },
    "list_tasks": {"project_id": {"type": "event_ref", "path": "project_id"}},
    "no_projection": {"project_id": {"type": "event_ref", "path": "project_id"}},
    "gate_create": {
        "task_id": {"type": "event_ref", "path": "task_id"},
        "title": {"type": "event_ref", "path": "title"},
    },
}


def build(command: str, contract: Any, *, save_as: str = "bound"):
    """A one-command, one-terminal artifact wired to *contract*."""
    artifact = load_artifact("two-rules-one-event.artifact.json")
    step = artifact.steps["ensure-review-task"]
    artifact = with_step(
        artifact,
        "ensure-review-task",
        step.model_copy(
            update={
                "command": command,
                "inputs": {
                    name: type(step.inputs["project_id"]).model_validate(payload)
                    if payload["type"] == "event_ref"
                    else payload
                    for name, payload in INPUTS[command].items()
                },
                "save_result_as": save_as,
                "transitions": {
                    name: target
                    for name, target in {
                        **{
                            spec.name: "review-done"
                            for spec in contract.execution.outcomes
                        },
                        "runtime_error": "review-failed",
                    }.items()
                },
            }
        ),
    )
    ref = artifact_ref_for(artifact)
    registry, adapter = registry_with(contract)
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


async def run_one(engine, ref):
    return await engine.run_rule(ref, "review", event("task-completed-code"), TRUSTED_LOCAL)


class TestCompleteness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("field_name", MANDATORY_FIELDS)
    async def test_receipt_carries_every_mandatory_field(self, field_name):
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        value = getattr(runs.receipts[0], field_name)
        assert value not in (None, "")

    @pytest.mark.asyncio
    async def test_artifact_sha256_on_every_receipt_matches_the_pinned_ref(self):
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        assert runs.receipts
        for receipt in runs.receipts:
            assert receipt.artifact_sha256 == ref.artifact_sha256

    @pytest.mark.asyncio
    async def test_a_command_receipt_pins_the_executed_contract(self):
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        assert runs.receipts[0].contract_fingerprint
        # A terminal has no contract, and does not borrow the command's.
        assert runs.receipts[-1].step_kind == "terminal"
        assert runs.receipts[-1].contract_fingerprint == ""

    @pytest.mark.asyncio
    async def test_each_attempt_has_a_distinct_four_part_identity(self):
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        keys = [r.idempotency_key for r in runs.receipts]
        assert len(keys) == len(set(keys))
        assert all(key.count(":") == 3 for key in keys)


class TestDefaultDenyRedaction:
    @pytest.mark.asyncio
    async def test_receipt_never_contains_an_unprojected_field(self):
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        projected = set(ENSURE_TASK.execution.receipt_projection)
        assert set(runs.receipts[0].result) <= projected

    @pytest.mark.asyncio
    async def test_empty_receipt_projection_projects_nothing(self):
        """``receipt_projection=()`` means *nothing*, even though the command
        returned a populated model."""
        engine, adapter, runs, ref = build("no_projection", NO_PROJECTION)
        adapter.queue.append(
            CommandResult(outcome="done", value=NoProjectionResult(), summary="")
        )
        await run_one(engine, ref)
        result = runs.receipts[0].result
        assert "populated" not in result
        assert set(result) <= {REDACTED_KEY}

    @pytest.mark.asyncio
    async def test_sensitive_field_is_hashed_not_masked_and_unallowed_is_dropped(self):
        engine, adapter, runs, ref = build("gate_create", GATE_CREATE, save_as="gate")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=GateCreateResult(gate_id="g-1", approved=True, secret_note="top secret"),
                summary="",
            )
        )
        await run_one(engine, ref)
        result = runs.receipts[0].result
        assert result["gate_id"] == "g-1"
        assert result["secret_note"].startswith(SENSITIVE_PREFIX)
        # ``approved`` is declared on the model but not projected: dropped,
        # not masked — a masked key still leaks that it was populated.
        assert "approved" not in result
        assert "top secret" not in str(result)

    @pytest.mark.asyncio
    async def test_inputs_are_redacted_wholesale_until_a_contract_declares_them(self):
        """Package 1 has no input-projection field yet, so the default is
        total redaction rather than a caller-chosen subset."""
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        inputs = runs.receipts[0].inputs
        assert set(inputs) <= {REDACTED_KEY}
        assert "Review: Add the widget" not in str(inputs)

    @pytest.mark.asyncio
    async def test_a_failed_step_projects_nothing_at_all(self):
        engine, adapter, runs, ref = build("list_tasks", LIST_TASKS, save_as="downstream")
        adapter.queue.append(RuntimeError("boom"))
        await run_one(engine, ref)
        assert runs.receipts[0].result == {}
        assert runs.receipts[0].error_code == "runtime_error"


class TestPrincipalProjection:
    @pytest.mark.asyncio
    async def test_the_receipt_records_the_identity_that_executed(self):
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        principal = runs.receipts[0].principal
        assert principal["kind"] == TRUSTED_LOCAL.kind.value
        assert set(principal) == {
            "kind",
            "profile_id",
            "session_id",
            "capability_fingerprint",
        }

    @pytest.mark.asyncio
    async def test_the_receipt_records_a_fingerprint_never_the_grant(self):
        """An operator needs to know the grant *changed* between two runs.
        Printing the grant itself would put a capability list into a surface
        Package 5 renders to anyone who can read the overlay."""
        engine, adapter, runs, ref = build("ensure_task", ENSURE_TASK, save_as="review")
        adapter.queue.append(
            CommandResult(
                outcome="created",
                value=EnsureTaskResult(task_id="t-1", created=True),
                summary="",
            )
        )
        await run_one(engine, ref)
        principal = runs.receipts[0].principal
        assert principal["capability_fingerprint"] == TRUSTED_LOCAL.policy.fingerprint()
        assert "ensure_task" not in str(principal)
