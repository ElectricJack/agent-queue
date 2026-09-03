"""Pure projection of a pinned V2 run and its durable receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from src.api.models.playbook_v2 import PlaybookRunOverlayResponse, ReceiptDTO
from src.event_schemas import event_field_is_sensitive
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.graph_projection import project_value


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if is_dataclass(value):
        return asdict(value)
    return dict(value)


def _artifact(ref: Any) -> dict[str, Any]:
    return ref.as_dict() if hasattr(ref, "as_dict") else dict(ref)


def _fingerprint(value: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _receipt(raw: Any) -> dict[str, Any]:
    """Accept both Package 4 ``StepReceipt`` and already-projected fixture rows."""
    data = _plain(raw)
    if "iteration_index" in data:
        return ReceiptDTO.model_validate(data).model_dump(mode="json")
    iteration = int(data.get("iteration", -1))
    started = float(data.get("started_at", 0.0))
    completed = data.get("completed_at")
    duration_ms = int(data.get("duration_ms", 0))
    inputs = []
    for name, value in dict(data.get("inputs") or {}).items():
        redacted = name == "__redacted__" or (
            isinstance(value, str) and value.startswith("sensitive:")
        )
        inputs.append(
            {
                "label": name.replace("_", " ").title(),
                "value": project_value(
                    value,
                    redacted=redacted,
                    type_name=type(value).__name__,
                ),
                "source": "derived",
                "required": True,
                "description": None,
            }
        )
    result_map = dict(data.get("result") or {})
    result = None
    if result_map:
        redacted = "__redacted__" in result_map or any(
            isinstance(value, str) and value.startswith("sensitive:")
            for value in result_map.values()
        )
        result = project_value(result_map, redacted=redacted, type_name="object")
    tokens_in = int(data.get("tokens_in", 0))
    tokens_out = int(data.get("tokens_out", 0))
    token_usage = None
    if tokens_in or tokens_out:
        token_usage = {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
            "estimated": False,
        }
    principal = dict(data.get("principal") or {})
    wait = None
    if data.get("wait_id"):
        wait = {
            "wait_kind": "human" if data.get("step_kind") == "wait" else "event",
            "correlation_key": str(data["wait_id"]),
            "registered_at": started,
            "deadline_at": None,
            "deadline_source": None,
            "matched_at": completed,
            "matched_event_id": None,
        }
    cancellation = None
    if data.get("cancelled_at") is not None:
        cancellation = {
            "requested_at": data["cancelled_at"],
            "acknowledged_at": completed,
            "cancelled_child": False,
        }
    selected = data.get("selected_transition")
    exact_outcome = (
        selected.rsplit("::", 1)[-1]
        if isinstance(selected, str) and "::" in selected
        else data.get("error_code") or data.get("outcome", "failure")
    )
    return ReceiptDTO.model_validate(
        {
            "receipt_id": data["receipt_id"],
            "step_id": data["step_id"],
            "rule_id": data["rule_id"],
            "step_kind": data["step_kind"],
            "attempt": int(data.get("attempt", 1)),
            "iteration_index": iteration if iteration >= 0 else None,
            "outcome": exact_outcome,
            "selected_edge_id": selected,
            "started_at": started,
            "completed_at": completed,
            "duration_seconds": duration_ms / 1000 if duration_ms else (
                float(completed) - started if completed is not None else None
            ),
            "inputs": inputs,
            "result": result,
            "token_usage": token_usage,
            "idempotency_key": data.get("idempotency_key") or None,
            "principal_fingerprint": principal.get("capability_fingerprint")
            or _fingerprint(principal),
            "profile_id": principal.get("profile_id"),
            "contract_fingerprint": data.get("contract_fingerprint") or None,
            "error": data.get("error"),
            "wait": wait,
            "cancellation": cancellation,
        }
    ).model_dump(mode="json")


def _state(receipts: list[dict[str, Any]]) -> str:
    last = receipts[-1]
    if last["completed_at"] is None:
        return "paused" if last.get("wait") else "running"
    outcome = last["outcome"]
    if outcome in {"cancelled", "canceled"}:
        return "cancelled"
    if outcome in {"timed_out", "timeout"}:
        return "timed_out"
    if outcome == "skipped":
        return "skipped"
    if last.get("error") or outcome in {
        "failure",
        "failed",
        "rejected",
        "operator_decision_required",
    }:
        return "failed"
    return "completed"


def redact_event(
    event: Mapping[str, Any], event_type: str, *, prefix: str = ""
) -> dict[str, Any]:
    """Redact registered sensitive fields recursively by dotted event path."""
    projected = {}
    for key, value in event.items():
        path = f"{prefix}.{key}" if prefix else key
        if event_field_is_sensitive(event_type, path):
            projected[key] = "(redacted)"
        elif isinstance(value, Mapping):
            projected[key] = redact_event(value, event_type, prefix=path)
        else:
            projected[key] = value
    return projected


def project_overlay(
    run: Any,
    receipts: list[Any],
    definition: PlaybookDefinition,
    artifact: Any,
    *,
    active_sha256: str | None,
    contracts: Any = None,
    receipt_limit: int = 500,
    receipt_total: int | None = None,
) -> dict[str, Any]:
    """Project newest receipts while preserving attempts and loop iterations."""
    del contracts  # receipts are already contract-projected at the execution boundary
    if receipt_limit < 1:
        raise ValueError("receipt_limit must be >= 1")
    definition = PlaybookDefinition.model_validate(definition)
    snapshot = run.redacted() if hasattr(run, "redacted") else run
    run_data = _plain(snapshot)
    total = len(receipts) if receipt_total is None else receipt_total
    selected = receipts[-receipt_limit:]
    projected = [_receipt(item) for item in selected]
    diagnostics = []
    known = set(definition.steps)
    by_step: dict[str, list[dict[str, Any]]] = {}
    for receipt in projected:
        if receipt["step_id"] not in known:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "unknown_receipt_step",
                    "message": f"Receipt {receipt['receipt_id']} names unknown step {receipt['step_id']!r}",
                    "rule_id": receipt["rule_id"],
                    "step_id": receipt["step_id"],
                    "source": None,
                }
            )
            continue
        by_step.setdefault(receipt["step_id"], []).append(receipt)
    nodes = []
    for step_id in definition.steps:
        visits = by_step.get(step_id, [])
        iterations = []
        for receipt in visits:
            index = receipt["iteration_index"]
            if index is None:
                continue
            item_display = f"Iteration {index + 1}"
            iterations.append(
                {
                    "index": index,
                    "item_display": item_display,
                    "outcome": receipt["outcome"],
                    "receipt_ids": [receipt["receipt_id"]],
                    "started_at": receipt["started_at"],
                    "completed_at": receipt["completed_at"],
                }
            )
        nodes.append(
            {
                "step_id": step_id,
                "state": _state(visits) if visits else "not_visited",
                "visit_count": len(visits),
                "last_outcome": visits[-1]["outcome"] if visits else None,
                "receipt_ids": [item["receipt_id"] for item in visits],
                "iterations": iterations,
            }
        )
    edge_visits: dict[str, list[dict[str, Any]]] = {}
    for receipt in projected:
        if receipt["selected_edge_id"]:
            edge_visits.setdefault(receipt["selected_edge_id"], []).append(receipt)
    edges = [
        {
            "edge_id": edge_id,
            "traversal_count": len(items),
            "last_traversed_at": max(
                item["completed_at"] or item["started_at"] for item in items
            ),
        }
        for edge_id, items in sorted(edge_visits.items())
    ]
    bindings = []
    for name, value in sorted(dict(run_data.get("bindings") or {}).items()):
        bindings.append(
            {
                "label": name,
                "value": project_value(value, type_name=type(value).__name__),
                "source": "binding",
                "required": True,
                "description": None,
            }
        )
    decision = run_data.get("operator_decision")
    if decision:
        decision = {
            "step_id": decision["step_id"],
            "attempt": decision["attempt"],
            "reason": decision["reason"],
            "options": list(decision.get("options") or ()),
            "raised_at": decision["raised_at"],
        }
    lifecycle = getattr(run_data.get("lifecycle"), "value", run_data.get("lifecycle", "running"))
    event_type = str(run_data.get("event_type") or "")
    response = {
        "success": True,
        "run_id": run_data["run_id"],
        "artifact": _artifact(artifact),
        "artifact_is_active": artifact.artifact_sha256 == active_sha256
        if hasattr(artifact, "artifact_sha256")
        else artifact.get("artifact_sha256") == active_sha256,
        "rule_id": run_data["rule_id"],
        "lifecycle": lifecycle,
        "current_step_id": run_data.get("current_step_id"),
        "started_at": run_data.get("started_at"),
        "completed_at": run_data.get("completed_at"),
        "deadline_at": run_data.get("deadline_at"),
        "trigger_event": redact_event(dict(run_data.get("event") or {}), event_type),
        "nodes": nodes,
        "edges": edges,
        "receipts": projected,
        "bindings": bindings,
        "operator_decision": decision,
        "budget": _plain(run_data["budget"]) if run_data.get("budget") is not None else None,
        "diagnostics": diagnostics,
        "truncated": total > receipt_limit,
        "receipt_total": total,
    }
    return PlaybookRunOverlayResponse.model_validate(response).model_dump(mode="json")


__all__ = ["project_overlay", "redact_event"]
