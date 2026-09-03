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
from src.playbooks.expressions import condition_values


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
    elif kind == "context_ref":
        path = getattr(value, "path", raw.get("path"))
        display = f"this run's {str(path).replace('.', ' ')}"
        dto_kind = "expression"
    elif kind == "coalesce":
        options = getattr(value, "options", raw.get("options", ()))
        display = " or else ".join(project_value(option)["display"] for option in options)
        dto_kind = "expression"
    elif kind == "list":
        items = raw.get("items", ()) if isinstance(raw, Mapping) else value.items
        display = "[" + ", ".join(project_value(item)["display"] for item in items) + "]"
        dto_kind = "expression"
    elif kind == "object":
        fields = getattr(value, "fields", raw.get("fields", {})) or {}
        display = (
            "{"
            + ", ".join(
                f"{name}: {project_value(field)['display']}" for name, field in fields.items()
            )
            + "}"
        )
        dto_kind = "expression"
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


#: §4.3's comparison vocabulary, rendered the way an operator reads it.
COMPARISON_TEXT = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "in": "in",
    "not_in": "not in",
    "contains": "contains",
}


def _condition_text(condition: Any) -> str:
    """Render a §4.3 condition as readable text rather than canonical JSON.

    The decision edge label and the decision card's input rows both show this;
    the canonical form stays available in ``advanced.typed_step``.
    """
    raw = _plain(condition)
    kind = getattr(condition, "type", None) or (
        raw.get("type") if isinstance(raw, Mapping) else None
    )

    def _field(name: str, default: Any = None) -> Any:
        found = getattr(condition, name, None)
        if found is not None:
            return found
        return raw.get(name, default) if isinstance(raw, Mapping) else default

    if kind == "comparison":
        op = str(_field("op", ""))
        left = project_value(_field("left"))["display"]
        right = project_value(_field("right"))["display"]
        return f"{left} {COMPARISON_TEXT.get(op, op)} {right}"
    if kind == "bool":
        op = str(_field("op", ""))
        operands = list(_field("operands", ()) or ())
        if op == "not":
            return f"not ({_condition_text(operands[0])})" if operands else "not ()"
        return f" {op} ".join(f"({_condition_text(item)})" for item in operands)
    if kind == "exists":
        display = project_value(_field("value"))["display"]
        return f"{display} is truthy" if _field("mode") == "truthy" else f"{display} is present"
    return _json(raw)


def _condition_source(condition: Any) -> str:
    """The strongest data source a condition reads, for its input row.

    ``condition_values`` yields nothing for anything but a typed condition, so
    an unrecognised shape degrades to ``"derived"`` rather than raising.
    """
    sources = {_value_source(value) for value in condition_values(condition)}
    for name in ("loop", "binding", "event", "template", "derived", "literal"):
        if name in sources:
            return name
    return "derived"


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


def _row(
    label: str,
    value: Any,
    *,
    source: str | None = None,
    required: bool = True,
    description: str | None = None,
    type_name: str | None = None,
) -> dict:
    """One labelled input/output row projected from a typed expression."""
    return {
        "label": label,
        "value": project_value(value, type_name=type_name),
        "source": _value_source(value) if source is None else source,
        "required": required,
        "description": description,
    }


def _effect(
    kind: str,
    subject: str,
    detail: str,
    *,
    arguments: list[dict] | None = None,
    conditional_on: str | None = None,
) -> dict:
    return {
        "kind": kind,
        "subject": subject,
        "detail": detail,
        "arguments": arguments or [],
        "conditional_on": conditional_on,
    }


def _input_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def _named_input_rows(step: Any) -> list[dict]:
    """The step's declared ``inputs`` mapping, in declaration order."""
    return [_row(_input_label(name), value) for name, value in getattr(step, "inputs", {}).items()]


#: What a family's ``save_result_as`` binding actually holds, for the row's
#: description.  Every family that can bind one has an entry.
_RESULT_DESCRIPTIONS = {
    "llm": "The model's structured output, bound for later steps to read.",
    "agent_task": "The delegated task's result, bound for later steps to read.",
    "wait": "What the wait resolved to, bound for later steps to read.",
    "foreach": "The loop's collected per-item results, bound for later steps to read.",
}


def _result_row(step: Any) -> dict | None:
    """The binding this step writes, or ``None`` when it writes nothing."""
    binding = getattr(step, "save_result_as", None)
    if not binding:
        return None
    schema = result_schema_for(step)
    type_name = schema.get("type") if isinstance(schema, Mapping) else None
    return {
        "label": str(binding),
        "value": project_value(
            {"type": "binding_ref", "binding": str(binding)},
            type_name=type_name or "object",
        ),
        "source": "derived",
        "required": True,
        "description": _RESULT_DESCRIPTIONS.get(step.type),
    }


def _binds_effect(step: Any) -> list[dict]:
    binding = getattr(step, "save_result_as", None)
    if not binding:
        return []
    return [_effect("binds", str(binding), f"Binds this step's result as {binding}")]


def _llm_explanation(step: LlmStep) -> tuple[str, list[dict], list[dict]]:
    summary = f"Ask the {step.profile_id} profile for a structured answer"
    if step.outcome_field:
        summary += f" and branch on its {step.outcome_field}"
    effects = [
        _effect(
            "invokes_ai",
            step.profile_id,
            f"Invokes the {step.profile_id} profile with a structured-output prompt, "
            f"capped at {step.budget.max_calls} call(s) and "
            f"{step.budget.max_total_tokens} total tokens",
        )
    ]
    if step.tool_use.enabled:
        effects.append(
            _effect(
                "invokes_ai",
                step.profile_id,
                "The model may call tools: "
                + (
                    ", ".join(sorted(step.tool_use.aq_commands + step.tool_use.plugin_tools))
                    or "none granted"
                ),
            )
        )
    effects.extend(_binds_effect(step))
    inputs = [_row("Prompt", step.prompt, type_name="string"), *_named_input_rows(step)]
    return summary, effects, inputs


def _agent_task_explanation(step: AgentTaskStep) -> tuple[str, list[dict], list[dict]]:
    waiting = "and wait for it" if step.wait_for_completion else "without waiting for it"
    summary = f"Delegate a task to the {step.profile_id} profile {waiting}"
    detail = f"Delegates a child agent task to the {step.profile_id} profile"
    if step.cancel_child:
        detail += "; the child is cancelled when this step is"
    effects = [_effect("delegates", step.profile_id, detail)]
    narrowing = step.capability_narrowing
    if narrowing is not None:
        narrowed = sorted(
            f"{namespace} ({len(granted)})"
            for namespace, granted in _plain(narrowing).items()
            if granted is not None
        )
        if narrowed:
            effects.append(
                _effect(
                    "delegates",
                    step.profile_id,
                    "Narrows the child's capabilities in " + ", ".join(narrowed),
                )
            )
    effects.extend(_binds_effect(step))
    inputs = [_row("Objective", step.objective, type_name="string"), *_named_input_rows(step)]
    return summary, effects, inputs


def _decision_explanation(step: DecisionStep, definition: PlaybookDefinition) -> tuple:
    default_title = getattr(definition.steps.get(step.default), "title", step.default)
    summary = (
        f"Take the first of {len(step.cases)} matching branch(es), "
        f"otherwise {default_title}"
    )
    effects = []
    inputs = []
    for index, case in enumerate(step.cases):
        label = case.label or f"Case {index + 1}"
        text = _condition_text(case.when)
        target = getattr(definition.steps.get(case.goto), "title", case.goto)
        effects.append(
            _effect("branches", case.goto, f"Goes to {target}", conditional_on=text)
        )
        inputs.append(
            {
                "label": label,
                "value": {
                    "kind": "expression",
                    "display": text,
                    "canonical": _plain(case.when),
                    "redacted": False,
                    "type_name": "boolean",
                },
                "source": _condition_source(case.when),
                "required": True,
                "description": None,
            }
        )
    effects.append(
        _effect("branches", step.default, f"Goes to {default_title} when no case matches")
    )
    return summary, effects, inputs


_WAIT_SUMMARIES = {
    "event": "Pause until a matching event arrives",
    "human": "Pause until a human resolves the gate",
    "task": "Pause until the awaited task finishes",
    "timer": "Pause for a fixed duration",
}


def _wait_explanation(step: WaitStep) -> tuple[str, list[dict], list[dict]]:
    summary = _WAIT_SUMMARIES[step.wait_kind]
    awaited = project_value(step.awaited)["display"] if step.awaited is not None else None
    detail = summary if awaited is None else f"{summary}: {awaited}"
    if step.wait_kind == "human" and step.outcomes:
        detail += f" (resolutions: {', '.join(step.outcomes)})"
    if step.timeout_seconds is not None:
        detail += f"; times out after {step.timeout_seconds}s"
    effects = [_effect("waits", step.wait_kind, detail)]
    effects.extend(_binds_effect(step))
    inputs = []
    if step.awaited is not None:
        inputs.append(_row("Awaited", step.awaited, type_name="string"))
    if step.correlation_key is not None:
        inputs.append(_row("Correlation key", step.correlation_key, type_name="string"))
    return summary, effects, inputs


def _foreach_explanation(step: ForEachStep, definition: PlaybookDefinition) -> tuple:
    collection = project_value(step.collection)["display"]
    body_title = getattr(definition.steps.get(step.body_entry), "title", step.body_entry)
    summary = f"Run {body_title} once per item in {collection}"
    policies = {
        "halt": "stops the loop at the first failing item",
        "continue": "skips a failing item and keeps going",
        "collect": "collects every failing item and reports them at the end",
    }
    effects = [
        _effect(
            "branches",
            step.item_binding,
            f"Iterates {collection} at most {step.max_iterations} times; "
            f"{policies[step.failure_policy]}",
        ),
        _effect("binds", step.item_binding, f"Binds each item as {step.item_binding}"),
    ]
    effects.extend(_binds_effect(step))
    inputs = [_row("Collection", step.collection, type_name="array")]
    return summary, effects, inputs


def _terminal_explanation(step: TerminalStep) -> tuple[str, list[dict], list[dict]]:
    summary = f"End the rule as {step.outcome}"
    detail = summary
    if step.result is not None:
        detail += f", returning {project_value(step.result)['display']}"
    return summary, [_effect("noop", "rule", detail)], []


def _canonical_explanation(step_id: str, step: Any, definition: PlaybookDefinition) -> dict:
    """The typed explanation of a non-command step.

    ``renderer="canonical"`` because no command contract supplies presentation
    metadata here — but the card is not therefore empty: every family projects
    the values it reads, the binding it writes, and what it does with them.
    """
    if isinstance(step, LlmStep):
        summary, effects, inputs = _llm_explanation(step)
    elif isinstance(step, AgentTaskStep):
        summary, effects, inputs = _agent_task_explanation(step)
    elif isinstance(step, DecisionStep):
        summary, effects, inputs = _decision_explanation(step, definition)
    elif isinstance(step, WaitStep):
        summary, effects, inputs = _wait_explanation(step)
    elif isinstance(step, ForEachStep):
        summary, effects, inputs = _foreach_explanation(step, definition)
    elif isinstance(step, TerminalStep):
        summary, effects, inputs = _terminal_explanation(step)
    else:  # pragma: no cover - the seven families are closed by the model
        raise GraphProjectionError(f"no explanation for step kind {step.type!r}")
    result = _result_row(step)
    if isinstance(step, TerminalStep) and step.result is not None:
        result = _row("Result", step.result, description="The rule's returned result.")
    return {
        "title": step.title,
        "effect_summary": summary,
        "effects": effects,
        "inputs": inputs,
        "result": result,
        "outcomes": _outcome_explanations(step_id, step, definition),
        "contract_fingerprint": None,
        "renderer": "canonical",
    }


def _routing(profiles: Any, step: Any, profile_id: str) -> Any | None:
    """The profile's resolved intelligence class / provider / model.

    Which question to ask depends on the step, because the two surfaces do
    not agree on the provider: an :class:`AgentTaskStep` launches a session,
    where the profile's harness names the CLI and so fixes the provider,
    while an :class:`LlmStep` is a headless direct-path call with no CLI,
    where ``llm.provider`` fixes it.  Asking ``routing`` for an ``LlmStep``
    is what made the card claim a provider and model slice the executor
    never used.

    Both methods are part of the ``ProfileLookup`` protocol, but a caller may
    still pass an older stub that answers only ``policy``, or only the
    session-surface ``routing``; such a lookup reports no routing rather
    than raising.
    """
    method = "direct_routing" if isinstance(step, LlmStep) else "routing"
    lookup = getattr(profiles, method, None)
    return lookup(profile_id) if callable(lookup) else None


def _ai_detail(step: Any, profiles: Any) -> dict | None:
    if not isinstance(step, (LlmStep, AgentTaskStep)):
        return None
    policy = profiles.policy(step.profile_id) if profiles is not None else None
    routing = _routing(profiles, step, step.profile_id) if profiles is not None else None
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


def _idempotency_badge(step: Any, registration: Any) -> dict | None:
    """The compact card's idempotency chip for a command step.

    The child plan's compact-card contract (§6.2) puts idempotency on every
    command card.  It has two sources and they are not interchangeable: a step
    may author its own ``idempotency_key``, which overrides whatever the
    contract declares, and otherwise the registered contract's mode is the
    answer.  ``none`` is stated rather than omitted — "running this twice does
    it twice" is exactly what an operator needs before re-dispatching an event
    — but an *unregistered* command has no answer at all: the node already
    carries an ``unknown_command`` diagnostic, and claiming "none" there would
    be a fact the projection does not have.
    """
    if not isinstance(step, CommandStep):
        return None
    if step.idempotency_key is not None:
        return {"kind": "idempotency", "label": "Idempotent", "value": "keyed by this step"}
    if registration is None:
        return None
    spec = registration.contract.execution.idempotency
    value = f"keyed on {spec.key_field}" if spec.mode == "keyed" else spec.mode
    return {"kind": "idempotency", "label": "Idempotent", "value": value}


def _badges(
    step: Any,
    *,
    registration: Any,
    retry: Any,
    diagnostics: list[dict],
) -> list[dict]:
    """The compact card's chips for one step, in reading order (§6.2).

    Everything a card shows about *what data* a step reads and writes comes
    from its explanation payload — these chips are the execution configuration
    beside it: who runs it, what it costs, how it retries, whether running it
    twice is safe.  The chip kinds are the frozen ``NodeBadgeDTO.kind`` set,
    so a new fact reuses an existing kind rather than widening a locked DTO.
    """
    badges: list[dict] = []
    if isinstance(step, (LlmStep, AgentTaskStep)):
        badges.append({"kind": "profile", "label": "Profile", "value": step.profile_id})
    if isinstance(step, LlmStep):
        badges.append(
            {
                "kind": "budget",
                "label": "Budget",
                "value": f"{step.budget.max_calls} call(s), "
                f"{step.budget.max_total_tokens} tokens",
            }
        )
        if step.tool_use.enabled:
            badges.append(
                {
                    "kind": "capability",
                    "label": "Tools",
                    "value": str(len(step.tool_use.aq_commands) + len(step.tool_use.plugin_tools)),
                }
            )
    if isinstance(step, AgentTaskStep):
        badges.append(
            {
                "kind": "wait",
                "label": "Waits",
                "value": "for completion" if step.wait_for_completion else "no",
            }
        )
        # Whether the rule takes the child down with it is a delegation fact an
        # operator reads off the card, not a detail of the wait.
        badges.append(
            {
                "kind": "wait",
                "label": "On cancel",
                "value": "cancels the child" if step.cancel_child else "leaves the child running",
            }
        )
    if isinstance(step, WaitStep):
        badges.append({"kind": "wait", "label": "Waits for", "value": step.wait_kind})
    if isinstance(step, ForEachStep):
        badges.append({"kind": "loop", "label": "Failure policy", "value": step.failure_policy})
    idempotency_badge = _idempotency_badge(step, registration)
    if idempotency_badge is not None:
        badges.append(idempotency_badge)
    if getattr(step, "timeout_seconds", None):
        badges.append({"kind": "timeout", "label": "Timeout", "value": f"{step.timeout_seconds}s"})
    if retry is not None:
        badges.append({"kind": "retry", "label": "Attempts", "value": str(retry.max_attempts)})
    if diagnostics:
        badges.append({"kind": "diagnostic", "label": "Errors", "value": str(len(diagnostics))})
    return badges


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
    badges = _badges(step, registration=registration, retry=retry, diagnostics=diagnostics)
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
