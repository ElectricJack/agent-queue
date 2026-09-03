"""Pure projection of a typed Playbook V2 artifact into the semantic graph DTO."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from src.api.models.playbook_v2 import PlaybookV2GraphResponse, StepExplanationDTO
from src.playbooks.definition import (
    AgentTaskStep,
    CommandStep,
    DecisionStep,
    ForEachStep,
    LlmStep,
    PlaybookDefinition,
    TerminalStep,
    WaitStep,
    reserved_outcomes_for,
    result_schema_for,
)
from src.playbooks.explanation import render_node_explanation


class GraphProjectionError(ValueError):
    """The artifact cannot be represented as closed rule clusters."""


STEP_LABELS = {
    "command": "Command",
    "llm": "AI",
    "agent_task": "Delegated task",
    "decision": "Decision",
    "wait": "Wait",
    "foreach": "For each",
    "terminal": "Terminal",
}
EDGE_LABELS = {
    "success": "Success",
    "failure": "Failure",
    "decision_case": "Case",
    "decision_default": "Default",
    "loop_body": "Loop body",
    "loop_exit": "Loop exit",
    "loop_back": "Next iteration",
    "timeout": "Timed out",
    "wait_matched": "Matched",
    "runtime_error": "Runtime error",
    "cancelled": "Cancelled",
    "terminal": "Terminal",
}


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _artifact_dict(artifact: Any) -> dict[str, Any]:
    raw = _plain(artifact)
    if not isinstance(raw, Mapping):
        raise TypeError("artifact reference must be a mapping or ArtifactRef")
    return dict(raw)


def _activation_dict(activation: Any, definition: PlaybookDefinition) -> dict[str, Any]:
    raw = _plain(activation) if activation is not None else {}
    raw = dict(raw) if isinstance(raw, Mapping) else {}
    health = getattr(raw.get("health"), "value", raw.get("health", "disabled"))
    reasons = [_plain(item) for item in raw.get("reasons", ())]
    scope_identifier = getattr(definition.scope, "project_id", None) or getattr(
        definition.scope, "agent_type", None
    )
    return {
        "playbook_id": raw.get("playbook_id", definition.id),
        "scope": raw.get("scope", definition.scope.type),
        "scope_identifier": raw.get("scope_identifier") or scope_identifier,
        "enabled": bool(raw.get("enabled", False)),
        "active_artifact_sha256": raw.get("active_artifact_sha256"),
        "health": health,
        "reasons": reasons,
        "activated_at": raw.get("activated_at"),
        "activated_by": raw.get("activated_by"),
        "pending_event_count": int(raw.get("pending_event_count", 0)),
        "running_count": int(raw.get("running_count", 0)),
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def project_value(value: Any, *, redacted: bool = False, type_name: str | None = None) -> dict:
    """Project one typed expression without resolving it against run data."""
    if redacted:
        return {
            "kind": "redacted",
            "display": "(redacted)",
            "canonical": None,
            "redacted": True,
            "type_name": type_name,
        }
    raw = _plain(value)
    kind = getattr(value, "type", None) or (raw.get("type") if isinstance(raw, Mapping) else None)
    if kind == "literal":
        literal = getattr(value, "value", raw.get("value"))
        display = literal if isinstance(literal, str) else _json(literal)
        dto_kind = "literal"
    elif kind == "event_ref":
        path = getattr(value, "path", raw.get("path"))
        display = f"this event's {str(path).replace('.', ' ')}"
        dto_kind = "event_ref"
    elif kind in {"binding_ref", "loop_ref"}:
        binding = getattr(value, "binding", raw.get("binding"))
        path = getattr(value, "path", raw.get("path"))
        display = f"{binding}{'.' + path if path else ''}"
        dto_kind = kind
    elif kind == "template":
        parts = getattr(value, "parts", raw.get("parts", ()))
        display = "".join(project_value(part)["display"] for part in parts)
        dto_kind = "template"
    else:
        display = _json(raw)
        dto_kind = "expression" if kind else "unresolved"
    return {
        "kind": dto_kind,
        "display": str(display),
        "canonical": raw if dto_kind != "unresolved" else None,
        "redacted": False,
        "type_name": type_name,
    }


def _value_source(value: Any) -> str:
    kind = getattr(value, "type", None)
    return {
        "event_ref": "event",
        "binding_ref": "binding",
        "loop_ref": "loop",
        "template": "template",
    }.get(kind, "literal" if kind == "literal" else "derived")


def _contract_info(contracts: Any, command: str) -> Any | None:
    return contracts.get(command) if contracts is not None else None


def _registration(contracts: Any, command: str) -> Any | None:
    registry = getattr(contracts, "_registry", contracts)
    found = registry.get(command) if registry is not None and hasattr(registry, "get") else None
    return found if hasattr(found, "contract") else None


def _command_explanation(
    step_id: str,
    step: CommandStep,
    rule: Any,
    definition: PlaybookDefinition,
    contracts: Any,
) -> tuple[dict, Any | None]:
    info = _contract_info(contracts, step.command)
    registration = _registration(contracts, step.command)
    contract = registration.contract if registration is not None else None
    labels = {name: item.title for name, item in definition.steps.items()}
    rendered = None
    if contract is not None:
        legacy_args = {name: _plain(value) for name, value in step.inputs.items()}
        rendered = render_node_explanation(
            step_id,
            {
                "action": {
                    "command": step.command,
                    "args": legacy_args,
                    "output": {"as": step.save_result_as} if step.save_result_as else None,
                }
            },
            event_type=rule.trigger.event_type,
            registry=getattr(contracts, "_registry", contracts),
            node_labels=labels,
        )
    if isinstance(rendered, Mapping):
        try:
            return StepExplanationDTO.model_validate(rendered).model_dump(mode="json"), info
        except Exception:  # noqa: BLE001 - old renderer payload is adapted below
            pass

    execution = contract.execution if contract is not None else None
    presentation = contract.presentation if contract is not None else None
    arguments = getattr(info, "arguments", {}) if info is not None else {}
    inputs = []
    sensitive = set(getattr(execution, "sensitive_args", ()))
    for name, value in step.inputs.items():
        spec = arguments.get(name) if isinstance(arguments, Mapping) else None
        field = execution.args_model.model_fields.get(name) if execution is not None else None
        label = (
            presentation.arg_labels.get(name)
            if presentation is not None
            else name.replace("_", " ").title()
        )
        type_name = getattr(getattr(spec, "type", None), "kind", None)
        if type_name is None and field is not None:
            type_name = getattr(field.annotation, "__name__", str(field.annotation))
        value_dto = (
            project_value(value, redacted=name in sensitive, type_name=type_name)
            if info is not None
            else {
                "kind": "unresolved",
                "display": "(unresolved)",
                "canonical": None,
                "redacted": False,
                "type_name": type_name,
            }
        )
        inputs.append(
            {
                "label": label or name,
                "value": value_dto,
                "source": _value_source(value),
                "required": bool(getattr(spec, "required", field.is_required() if field else True)),
                "description": None,
            }
        )
    effect_rows = []
    if rendered is not None:
        effect_kind = {
            "create": "creates",
            "create_or_reuse": "creates",
            "reuse": "reads",
            "update": "updates",
            "link": "updates",
            "resolve": "updates",
            "read": "reads",
        }
        for effect in rendered.effects:
            effect_rows.append(
                {
                    "kind": effect_kind.get(effect.operation, "noop"),
                    "subject": effect.subject or step.command,
                    "detail": effect.text,
                    "arguments": [],
                    "conditional_on": effect.condition,
                }
            )
    outcomes = _outcome_explanations(step_id, step, definition, contract)
    result = None
    if step.save_result_as:
        result = {
            "label": str(step.save_result_as),
            "value": project_value(
                {"type": "binding_ref", "binding": str(step.save_result_as)},
                type_name="object",
            ),
            "source": "derived",
            "required": True,
            "description": None,
        }
    return {
        "title": presentation.title if presentation is not None else step.title,
        "effect_summary": (
            presentation.summary if presentation is not None else f"Invoke {step.command}"
        ),
        "effects": effect_rows,
        "inputs": inputs,
        "result": result,
        "outcomes": outcomes,
        "contract_fingerprint": (
            getattr(info, "execution_fingerprint", None)
            or (contract.fingerprint() if contract is not None else None)
        ),
        "renderer": "contract" if contract is not None else "canonical",
    }, info


def _outcome_explanations(
    step_id: str, step: Any, definition: PlaybookDefinition, contract: Any | None = None
) -> list[dict]:
    rows = []
    for edge in _step_edges(step_id, step, definition):
        target = definition.steps.get(edge["target"])
        label = edge["label"]
        if contract is not None:
            label = contract.presentation.outcome_labels.get(edge["outcome"], label)
        rows.append(
            {
                "outcome": edge["outcome"],
                "label": label,
                "target_step_id": edge["target"],
                "target_title": target.title if target is not None else None,
                "reserved": edge["reserved"],
                "terminal_outcome": target.outcome if isinstance(target, TerminalStep) else None,
            }
        )
    if isinstance(step, TerminalStep):
        rows.append(
            {
                "outcome": step.outcome,
                "label": step.outcome.replace("_", " ").title(),
                "target_step_id": None,
                "target_title": None,
                "reserved": False,
                "terminal_outcome": step.outcome,
            }
        )
    return rows


def _condition_text(condition: Any) -> str:
    return _json(_plain(condition))


def _edge_kind(step: Any, outcome: str, target: Any) -> str:
    if outcome == "runtime_error":
        return "runtime_error"
    if outcome in {"timed_out", "timeout"}:
        return "timeout"
    if outcome == "cancelled":
        return "cancelled"
    if outcome in {
        "input_resolution_failed",
        "unavailable",
        "contract_violation",
        "state_limit_exceeded",
        "interrupted",
        "invalid_output",
        "budget_exceeded",
        "provider_error",
    }:
        return "runtime_error"
    if isinstance(step, WaitStep) and outcome not in {"runtime_error", "timed_out"}:
        return "wait_matched"
    if isinstance(step, ForEachStep) and outcome == "body":
        return "loop_body"
    if isinstance(step, ForEachStep) and outcome == "completed":
        return "loop_exit"
    if isinstance(target, ForEachStep) and isinstance(step, DecisionStep):
        return "loop_back"
    if outcome in {"success", "created", "reused", "listed", "completed", "dispatched"}:
        return "success"
    if outcome in {"failure", "failed", "rejected", "unavailable"}:
        return "failure"
    return "success"


def _step_edges(step_id: str, step: Any, definition: PlaybookDefinition) -> list[dict]:
    records: list[tuple[str, str, str | None, str | None]] = []
    if isinstance(step, DecisionStep):
        records.extend(
            (f"case:{index}", case.goto, case.label, _condition_text(case.when))
            for index, case in enumerate(step.cases)
        )
        records.append(("default", step.default, "Default", None))
    else:
        if isinstance(step, ForEachStep):
            records.append(("body", step.body_entry, "Each item", None))
        records.extend(
            (str(outcome), str(target), None, None)
            for outcome, target in getattr(step, "transitions", {}).items()
        )
    reserved = reserved_outcomes_for(step) if not isinstance(step, TerminalStep) else frozenset()
    result = []
    for outcome, target_id, label, condition in records:
        target = definition.steps.get(target_id)
        result.append(
            {
                "id": f"{step.rule}::{step_id}::{outcome}",
                "rule_id": str(step.rule),
                "source": step_id,
                "source_port": outcome,
                "target": target_id,
                "outcome": outcome,
                "label": label or outcome.replace("_", " ").title(),
                "kind": (
                    "decision_default"
                    if isinstance(step, DecisionStep) and outcome == "default"
                    else "decision_case"
                    if isinstance(step, DecisionStep)
                    else _edge_kind(step, outcome, target)
                ),
                "reserved": outcome == "runtime_error" or outcome in reserved,
                "condition": condition,
            }
        )
    return result


def project_edges(definition: PlaybookDefinition) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for rule in definition.rules:
        owned = [step_id for step_id, step in definition.steps.items() if step.rule == rule.id]
        owned_set = set(owned)
        for step_id in owned:
            for edge in _step_edges(step_id, definition.steps[step_id], definition):
                if edge["target"] not in owned_set:
                    raise GraphProjectionError(
                        f"edge {edge['id']} crosses rule cluster {rule.id}: {edge['target']}"
                    )
                edges.append(edge)
    return edges


def _canonical_explanation(step_id: str, step: Any, definition: PlaybookDefinition) -> dict:
    effect = {
        "llm": ("invokes_ai", "Invoke AI"),
        "agent_task": ("delegates", "Delegate a task"),
        "decision": ("branches", "Choose a branch"),
        "wait": ("waits", "Wait for a condition"),
        "foreach": ("branches", "Iterate a collection"),
        "terminal": ("noop", "End the rule"),
    }[step.type]
    return {
        "title": step.title,
        "effect_summary": effect[1],
        "effects": [
            {
                "kind": effect[0],
                "subject": step.type,
                "detail": effect[1],
                "arguments": [],
                "conditional_on": None,
            }
        ],
        "inputs": [],
        "result": None,
        "outcomes": _outcome_explanations(step_id, step, definition),
        "contract_fingerprint": None,
        "renderer": "canonical",
    }


def _routing(profiles: Any, profile_id: str) -> Any | None:
    """The profile's resolved intelligence class / provider / model.

    ``routing`` is part of the ``ProfileLookup`` protocol, but a caller may
    still pass an older stub that only answers ``policy``; such a lookup
    reports no routing rather than raising.
    """
    lookup = getattr(profiles, "routing", None)
    return lookup(profile_id) if callable(lookup) else None


def _ai_detail(step: Any, profiles: Any) -> dict | None:
    if not isinstance(step, (LlmStep, AgentTaskStep)):
        return None
    policy = profiles.policy(step.profile_id) if profiles is not None else None
    routing = _routing(profiles, step.profile_id) if profiles is not None else None
    capabilities = {
        "harness_tools": sorted(getattr(policy, "harness_tools", ())),
        "aq_commands": sorted(getattr(policy, "aq_commands", ())),
        "plugin_tools": sorted(getattr(policy, "plugin_tools", ())),
    }
    budget = _plain(step.budget) if isinstance(step, LlmStep) else {}
    narrowing = getattr(step, "capability_narrowing", None)
    delegation = None
    if isinstance(step, AgentTaskStep):
        delegation = {
            "child_profile_id": step.profile_id,
            "wait_for_completion": step.wait_for_completion,
            "cancel_child": step.cancel_child,
            "narrowed_from": None,
            "capability_narrowing": _plain(narrowing) if narrowing is not None else None,
        }
    return {
        "profile_id": step.profile_id,
        "intelligence_class": getattr(routing, "intelligence_class", None),
        "provider": getattr(routing, "provider", None),
        "model": getattr(routing, "model", None),
        "capabilities": capabilities,
        "capability_fingerprint": policy.fingerprint() if policy is not None else "",
        "budget": budget,
        "output_schema": step.output_schema if isinstance(step, LlmStep) else None,
        "tool_use_enabled": bool(step.tool_use.enabled) if isinstance(step, LlmStep) else False,
        "delegation": delegation,
    }


def _node(
    step_id: str,
    step: Any,
    rule: Any,
    definition: PlaybookDefinition,
    contracts: Any,
    profiles: Any,
    out_degree: int,
) -> dict:
    diagnostics = []
    contract_info = None
    if isinstance(step, CommandStep):
        explanation, contract_info = _command_explanation(
            step_id, step, rule, definition, contracts
        )
        if contract_info is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "unknown_command",
                    "message": f"Command {step.command!r} is not registered",
                    "rule_id": rule.id,
                    "step_id": step_id,
                    "source": _plain(step.source),
                }
            )
    else:
        explanation = _canonical_explanation(step_id, step, definition)

    typed_step = _plain(step)
    registration = _registration(contracts, step.command) if isinstance(step, CommandStep) else None
    sensitive = set(
        getattr(registration.contract.execution, "sensitive_args", ())
        if registration is not None
        else ()
    )
    if isinstance(step, CommandStep) and sensitive:
        typed_step = dict(typed_step)
        typed_step["inputs"] = {
            name: ({"type": "redacted"} if name in sensitive else raw)
            for name, raw in typed_step.get("inputs", {}).items()
        }
    elif isinstance(step, CommandStep) and contract_info is None:
        typed_step = dict(typed_step)
        typed_step["inputs"] = {
            name: {"type": "unresolved"} for name in typed_step.get("inputs", {})
        }
    retry = getattr(step, "retry", None)
    retry_dto = _plain(retry) if retry is not None else None
    idempotency = None
    if isinstance(step, CommandStep):
        execution = registration.contract.execution if registration is not None else None
        idempotency = {
            "supported": bool(execution and execution.idempotency.mode != "none"),
            "key_template": _json(_plain(step.idempotency_key)) if step.idempotency_key else None,
            "retry_safe": bool(execution and execution.retry_safe),
        }
    advanced = {
        "typed_step": typed_step,
        "resolved_inputs": explanation["inputs"],
        "result_schema": (
            dict(getattr(contract_info, "result_schema", {}))
            if isinstance(step, CommandStep) and contract_info is not None
            else result_schema_for(step)
        ),
        "retry": retry_dto,
        "idempotency": idempotency,
        "redaction": [
            {"field": name, "policy": "redacted" if name in sensitive else "safe"}
            for name in sorted(getattr(step, "inputs", {}))
        ],
        "execution_fingerprint": getattr(contract_info, "execution_fingerprint", None),
    }
    loop = None
    if isinstance(step, ForEachStep):
        loop = {
            "collection": project_value(step.collection),
            "item_binding": step.item_binding,
            "failure_policy": step.failure_policy,
            "body_entry_step_id": step.body_entry,
            "continuation_step_id": step.continuation,
        }
    wait = None
    if isinstance(step, WaitStep):
        wait = {
            "wait_kind": step.wait_kind,
            "awaited": project_value(step.awaited)["display"] if step.awaited else step.wait_kind,
            "correlation_key": project_value(step.correlation_key),
            "timeout_seconds": step.timeout_seconds,
            "timeout_step_id": step.transitions.get("timed_out"),
        }
    badges = []
    if isinstance(step, (LlmStep, AgentTaskStep)):
        badges.append({"kind": "profile", "label": "Profile", "value": step.profile_id})
    if getattr(step, "timeout_seconds", None):
        badges.append(
            {"kind": "timeout", "label": "Timeout", "value": f"{step.timeout_seconds}s"}
        )
    if retry is not None:
        badges.append(
            {"kind": "retry", "label": "Attempts", "value": str(retry.max_attempts)}
        )
    if diagnostics:
        badges.append({"kind": "diagnostic", "label": "Errors", "value": str(len(diagnostics))})
    return {
        "id": step_id,
        "rule_id": rule.id,
        "step_kind": step.type,
        "title": step.title,
        "description": step.description,
        "entry": rule.entry_step == step_id,
        "terminal_outcome": step.outcome if isinstance(step, TerminalStep) else None,
        "explanation": explanation,
        "badges": badges,
        "ai": _ai_detail(step, profiles),
        "loop": loop,
        "wait": wait,
        "source": _plain(step.source),
        "advanced": advanced,
        "diagnostics": diagnostics,
        "out_degree": out_degree,
        "position": {"x": 0, "y": 0},
    }


def _layout(rules: list[Any], node_ids: dict[str, list[str]], edges: list[dict], direction: str):
    positions: dict[str, dict[str, int]] = {}
    bounds: dict[str, dict[str, int]] = {}
    cluster_offset = 0
    for rule in rules:
        owned = node_ids[rule.id]
        outgoing = {node_id: [] for node_id in owned}
        for edge in edges:
            if edge["rule_id"] == rule.id:
                outgoing[edge["source"]].append(edge["target"])
        depth = {rule.entry_step: 0}
        queue = deque([rule.entry_step])
        while queue:
            source = queue.popleft()
            for target in outgoing.get(source, ()):
                if target not in depth:
                    depth[target] = depth[source] + 1
                    queue.append(target)
        for node_id in owned:
            depth.setdefault(node_id, max(depth.values(), default=-1) + 1)
        per_depth: dict[int, list[str]] = {}
        for node_id in owned:
            per_depth.setdefault(depth[node_id], []).append(node_id)
        width = max((len(row) for row in per_depth.values()), default=1)
        height = max(per_depth, default=0) + 1
        for layer, row in sorted(per_depth.items()):
            for lane, node_id in enumerate(row):
                if direction == "TD":
                    positions[node_id] = {"x": lane, "y": cluster_offset + layer}
                else:
                    positions[node_id] = {"x": cluster_offset + layer, "y": lane}
        if direction == "TD":
            bounds[rule.id] = {"x": 0, "y": cluster_offset, "width": width, "height": height}
            cluster_offset += height + 1
        else:
            bounds[rule.id] = {"x": cluster_offset, "y": 0, "width": height, "height": width}
            cluster_offset += height + 1
    return positions, bounds


def project_graph(
    definition: PlaybookDefinition,
    artifact: Any,
    activation: Any,
    *,
    event_type: str | None = None,
    contracts: Any = None,
    profiles: Any = None,
    direction: str = "TD",
) -> dict[str, Any]:
    """Return a deterministic, DTO-validated graph without I/O."""
    definition = PlaybookDefinition.model_validate(definition)
    direction = direction.upper()
    if direction not in {"TD", "LR"}:
        raise GraphProjectionError("direction must be TD or LR")
    all_edges = project_edges(definition)
    all_rule_nodes = {
        rule.id: [step_id for step_id, step in definition.steps.items() if step.rule == rule.id]
        for rule in definition.rules
    }
    event_groups = []
    for kind in dict.fromkeys(rule.trigger.event_type for rule in definition.rules):
        ids = [rule.id for rule in definition.rules if rule.trigger.event_type == kind]
        event_groups.append(
            {
                "event_type": kind,
                "rule_ids": ids,
                "node_count": sum(len(all_rule_nodes[item]) for item in ids),
                "edge_count": sum(edge["rule_id"] in ids for edge in all_edges),
            }
        )
    selected_rules = [
        rule for rule in definition.rules if event_type is None or rule.trigger.event_type == event_type
    ]
    selected_ids = {rule.id for rule in selected_rules}
    edges = [edge for edge in all_edges if edge["rule_id"] in selected_ids]
    positions, bounds = _layout(selected_rules, all_rule_nodes, edges, direction)
    nodes = []
    for rule in selected_rules:
        for step_id in all_rule_nodes[rule.id]:
            node = _node(
                step_id,
                definition.steps[step_id],
                rule,
                definition,
                contracts,
                profiles,
                sum(edge["source"] == step_id for edge in edges),
            )
            node["position"] = positions[step_id]
            nodes.append(node)
    rules = [
        {
            "rule_id": rule.id,
            "name": rule.name,
            "event_type": rule.trigger.event_type,
            "trigger_filter": rule.trigger.filter,
            "entry_step_id": rule.entry_step,
            "step_ids": all_rule_nodes[rule.id],
            "source": _plain(rule.source),
            "diagnostics": [],
        }
        for rule in selected_rules
    ]
    diagnostics = [item for node in nodes for item in node["diagnostics"]]
    response = {
        "success": True,
        "artifact": _artifact_dict(artifact),
        "activation": _activation_dict(activation, definition),
        "purpose": definition.purpose,
        "event_groups": event_groups,
        "rules": rules,
        "nodes": nodes,
        "edges": edges,
        "layout": {
            "direction": direction,
            "grid_positions": positions,
            "cluster_bounds": bounds,
        },
        "diagnostics": diagnostics,
        "legend": {"step_kinds": STEP_LABELS, "edge_kinds": EDGE_LABELS},
    }
    return PlaybookV2GraphResponse.model_validate(response).model_dump(mode="json")


__all__ = ["GraphProjectionError", "project_edges", "project_graph", "project_value"]
