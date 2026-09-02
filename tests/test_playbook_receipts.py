"""Execution receipts — attempt identity and default-deny projection.

Package 3 child plan §9.1 (the four-part idempotency key) and §8.3 (the
receipt projection).  Nothing here touches the database: a receipt is a
value, and what may appear in it is decided before it is ever persisted.
"""

from __future__ import annotations

import pytest

from src.playbooks.receipts import (
    StepReceipt,
    idempotency_key,
    project_receipt,
    sensitive_handle,
)


def test_idempotency_key_includes_the_loop_iteration():
    """§9.1: the design spec's three-part key collides across iterations."""
    first = idempotency_key("run-1", "call-api", 0, 1)
    second = idempotency_key("run-1", "call-api", 1, 1)
    assert first != second
    assert first == "run-1:call-api:0:1"
    assert second == "run-1:call-api:1:1"


def test_idempotency_key_marks_a_non_loop_step_with_a_dash():
    assert idempotency_key("run-1", "step", -1, 1) == "run-1:step:-:1"


def test_idempotency_key_separates_attempts():
    assert idempotency_key("run-1", "step", -1, 1) != idempotency_key("run-1", "step", -1, 2)


def test_receipt_fills_its_idempotency_key():
    receipt = StepReceipt(
        receipt_id="r1",
        run_id="run-1",
        artifact_sha256="sha256:" + "a" * 64,
        rule_id="on-task-completed",
        step_id="call-api",
        step_kind="command",
        outcome="success",
        started_at=1.0,
        snapshot_version=3,
        iteration=2,
        attempt=4,
    )
    assert receipt.idempotency_key == "run-1:call-api:2:4"


def test_receipt_rejects_an_outcome_outside_the_check_constraint():
    with pytest.raises(ValueError, match="outcome"):
        StepReceipt(
            receipt_id="r1",
            run_id="run-1",
            artifact_sha256="sha256:" + "a" * 64,
            rule_id="r",
            step_id="s",
            step_kind="command",
            outcome="maybe",
            started_at=1.0,
            snapshot_version=0,
        )


def test_projection_is_default_deny():
    """§8.3: with every argument at its default, nothing is projected."""
    inputs = {"token": "hunter2", "task_id": "t-1"}
    result = {"secret": "s3cr3t", "count": 2}

    projected_inputs, projected_result = project_receipt(inputs, result)

    assert projected_inputs == {"__redacted__": 2}
    assert projected_result == {"__redacted__": 2}
    assert "hunter2" not in str(projected_inputs)
    assert "s3cr3t" not in str(projected_result)


def test_projection_copies_only_allow_listed_result_fields():
    _, projected = project_receipt(
        {},
        {"count": 2, "secret": "s3cr3t"},
        receipt_projection=("count",),
    )
    assert projected == {"count": 2}


def test_projection_omits_an_allow_listed_key_the_result_lacks():
    _, projected = project_receipt({}, {"count": 2}, receipt_projection=("count", "missing"))
    assert projected == {"count": 2}


def test_sensitive_value_is_replaced_by_a_stable_handle():
    _, first = project_receipt(
        {},
        {"api_key": "s3cr3t"},
        receipt_projection=("api_key",),
        sensitive_result_fields=("api_key",),
        run_id="run-1",
    )
    _, second = project_receipt(
        {},
        {"api_key": "s3cr3t"},
        receipt_projection=("api_key",),
        sensitive_result_fields=("api_key",),
        run_id="run-1",
    )
    assert first == second
    assert first["api_key"].startswith("sensitive:")
    assert "s3cr3t" not in first["api_key"]


def test_sensitive_handle_is_scoped_to_the_run():
    a = sensitive_handle("run-1", "result.api_key", "s3cr3t")
    b = sensitive_handle("run-2", "result.api_key", "s3cr3t")
    assert a != b
    assert a == sensitive_handle("run-1", "result.api_key", "s3cr3t")


def test_input_projection_redacts_sensitive_arguments():
    projected, _ = project_receipt(
        {"task_id": "t-1", "token": "hunter2"},
        {},
        input_projection=("task_id", "token"),
        sensitive_args=("token",),
        run_id="run-1",
    )
    assert projected["task_id"] == "t-1"
    assert projected["token"].startswith("sensitive:")
    assert "hunter2" not in str(projected)
