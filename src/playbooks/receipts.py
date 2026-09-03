"""Execution receipts — one immutable record per durable boundary.

Package 3 child plan §9 (attempt identity) and §8.3 (the projection).

Two things make a receipt safe to show an operator:

* **Attempt identity is four-part** — run, step, loop iteration, attempt.
  The design spec's prose gives a three-part key, which collides across
  iterations of the same step inside a ``ForEachStep`` and would suppress the
  second iteration's side effect as a duplicate (§9.1, amendment 2).  The
  Receipt identity extends it with turn index and receipt kind.  The database
  enforces that independently through ``uq_playbook_step_receipts_boundary``,
  so a hand-built key cannot get past it either.
* **Projection is default-deny.**  With no contract classification supplied,
  a receipt records how many inputs and results existed and nothing about
  what they were.  Widening it takes a contract field, not a caller argument.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The six-value ``ck_playbook_step_receipts_outcome`` CHECK, in its order.
RECEIPT_OUTCOMES: tuple[str, ...] = (
    "success",
    "failure",
    "skipped",
    "timeout",
    "cancelled",
    "operator_decision_required",
)

RECEIPT_KINDS: tuple[str, ...] = (
    "step",
    "tool_turn",
    "llm_call",
    "interrupted",
    "operator_decision",
)

#: Key used when a mapping is redacted wholesale.
REDACTED_KEY = "__redacted__"

#: Prefix of every opaque sensitive handle.
SENSITIVE_PREFIX = "sensitive:"


def idempotency_key(run_id: str, step_id: str, iteration: int, attempt: int) -> str:
    """Deterministic attempt identity (roadmap Package 3: run, step, loop
    iteration, attempt)."""
    loop_part = "-" if iteration < 0 else str(iteration)
    return f"{run_id}:{step_id}:{loop_part}:{attempt}"


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def sensitive_handle(run_id: str, path: str, value: Any) -> str:
    """An opaque, stable stand-in for a value that must never be receipted.

    Scoped to the run so the same secret in two runs does not correlate them,
    and to the path so two fields of one result stay distinguishable.
    """
    digest = hashlib.sha256(f"{run_id}|{path}|{_canonical(value)}".encode()).hexdigest()
    return f"{SENSITIVE_PREFIX}{digest[:32]}"


def project_receipt(
    inputs: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    receipt_projection: Sequence[str] = (),
    sensitive_args: Collection[str] = (),
    sensitive_result_fields: Collection[str] = (),
    input_projection: Sequence[str] | None = None,
    run_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reduce a step's inputs and result to what a receipt may carry.

    ``receipt_projection``, ``sensitive_args`` and ``sensitive_result_fields``
    come straight off Package 1's ``ExecutionContract``; ``input_projection``
    has no contract field yet (§21), so inputs are redacted wholesale until
    one exists.  Every argument defaults to "classify nothing", and the
    default is total redaction — there is no path by which a caller widens
    the projection beyond what the contract declares.
    """
    projected_inputs: dict[str, Any] = {}
    if input_projection is None:
        projected_inputs = {REDACTED_KEY: len(inputs)}
    else:
        sensitive_input_names = set(sensitive_args)
        for name in input_projection:
            if name not in inputs:
                continue
            value = inputs[name]
            projected_inputs[name] = (
                sensitive_handle(run_id, f"inputs.{name}", value)
                if name in sensitive_input_names
                else value
            )

    projected_result: dict[str, Any] = {}
    if not receipt_projection:
        projected_result = {REDACTED_KEY: len(result)}
    else:
        sensitive_result_names = set(sensitive_result_fields)
        for name in receipt_projection:
            if name not in result:
                continue
            value = result[name]
            projected_result[name] = (
                sensitive_handle(run_id, f"result.{name}", value)
                if name in sensitive_result_names
                else value
            )

    return projected_inputs, projected_result


@dataclass(frozen=True, slots=True)
class StepReceipt:
    """One durable boundary within one step attempt."""

    receipt_id: str
    run_id: str
    artifact_sha256: str
    rule_id: str
    step_id: str
    step_kind: str
    outcome: str
    started_at: float
    snapshot_version: int
    receipt_kind: str = "step"
    turn_index: int = -1
    operator_decision_id: str | None = None
    iteration: int = -1
    attempt: int = 1
    idempotency_key: str = ""
    contract_fingerprint: str = ""
    principal: Mapping[str, Any] = field(default_factory=dict)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    selected_transition: str | None = None
    error: str | None = None
    error_code: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    wait_id: str | None = None
    timed_out: bool = False
    cancelled_at: float | None = None
    completed_at: float | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in RECEIPT_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {list(RECEIPT_OUTCOMES)}, got {self.outcome!r}"
            )
        if self.receipt_kind not in RECEIPT_KINDS:
            raise ValueError(
                f"receipt_kind must be one of {list(RECEIPT_KINDS)}, "
                f"got {self.receipt_kind!r}"
            )
        if self.turn_index < -1:
            raise ValueError("turn_index must be >= -1")
        if (self.receipt_kind == "step") != (self.turn_index == -1):
            raise ValueError("step receipts use turn_index -1; turn boundaries use >= 0")
        decision_boundary = self.receipt_kind in {"interrupted", "operator_decision"}
        if decision_boundary != bool(self.operator_decision_id):
            raise ValueError(
                "interrupted/operator_decision receipts require operator_decision_id only"
            )
        if self.attempt < 1:
            raise ValueError("attempt must be >= 1")
        if not self.idempotency_key:
            object.__setattr__(
                self,
                "idempotency_key",
                idempotency_key(self.run_id, self.step_id, self.iteration, self.attempt),
            )


def transition_id(rule_id: str, step_id: str, outcome: str) -> str:
    """``selected_transition``'s value — Package 5's overlay joins on it."""
    return f"{rule_id}::{step_id}::{outcome}"
