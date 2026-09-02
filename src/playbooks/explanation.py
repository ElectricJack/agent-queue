"""Pure, contract-derived explanations for current playbook nodes."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, assert_never

from src.commands.contracts.models import (
    REDACTED,
    CommandPresentation,
    CreateClause,
    CreateOrReuseClause,
    EffectClause,
    LinkClause,
    ReadClause,
    ResolveClause,
    ReuseClause,
    UpdateClause,
    redact_args,
)
from src.commands.contracts.registry import CONTRACTS, ContractRegistry
from src.api.models.playbook import (
    ExplanationEffect,
    ExplanationInput,
    ExplanationLoop,
    ExplanationOutcome,
    ExplanationResultBinding,
    ExplanationValue,
    NodeExplanation,
)
from src.event_schemas import event_field_is_sensitive, resolve_event_path

logger = logging.getLogger(__name__)
_REF = re.compile(r"{{\s*([^{}]+?)\s*}}")
_EVENT_REF = re.compile(r"{{\s*event\.([^{}\s]+)\s*}}")


def can_render(clause: EffectClause) -> bool:
    return isinstance(
        clause,
        (
            CreateClause,
            ReuseClause,
            CreateOrReuseClause,
            UpdateClause,
            LinkClause,
            ResolveClause,
            ReadClause,
        ),
    )


def render_effect(
    clause: EffectClause, args: Mapping[str, Any], presentation: CommandPresentation
) -> ExplanationEffect:
    subject = presentation.subject_labels.get(
        clause.subject.value, clause.subject.value.replace("_", " ")
    )
    condition = f"when {clause.when.arg_present} is provided" if clause.when.arg_present else None
    if clause.when.arg_equals:
        condition = (
            f"when {clause.when.arg_equals[0]} equals {json.dumps(clause.when.arg_equals[1])}"
        )
    match clause:
        case CreateClause():
            text = f"Create {subject}"
        case ReuseClause(key_arg=key):
            text = f"Reuse {subject}" + (f" keyed by {json.dumps(key)}" if key else "")
        case CreateOrReuseClause(key_arg=key):
            text = f"Create or reuse {subject} keyed by {json.dumps(key)}"
        case UpdateClause():
            text = f"Update {subject}"
        case LinkClause():
            text = f"Link {subject}"
        case ResolveClause():
            text = f"Resolve {subject}"
        case ReadClause():
            text = f"Read {subject}"
        case _ as unreachable:
            assert_never(unreachable)
    return ExplanationEffect(
        operation=clause.kind, text=text, condition=condition, subject=clause.subject.value
    )


def _event_value(reference: str, event_type: str | None) -> ExplanationValue:
    path = reference.removeprefix("event.")
    spec = resolve_event_path(event_type or "", path)
    text = (
        f"this event's {spec['description']}" if spec else f"this event's {path.replace('.', ' ')}"
    )
    return ExplanationValue(kind="event_ref", text=text, raw="{{" + reference + "}}")


def _value(raw: Any, *, event_type: str | None, loop_name: str | None) -> ExplanationValue:
    if isinstance(raw, list) and len(raw) == 1:
        return _value(raw[0], event_type=event_type, loop_name=loop_name)
    if not isinstance(raw, str):
        return ExplanationValue(kind="literal", text=json.dumps(raw, ensure_ascii=False))
    matches = list(_REF.finditer(raw))
    if not matches:
        return ExplanationValue(kind="literal", text=raw or '""')
    if len(matches) == 1 and matches[0].span() == (0, len(raw)):
        reference = matches[0].group(1).strip()
        if reference.startswith("event."):
            return _event_value(reference, event_type)
        if reference.startswith("outputs."):
            pieces = reference.split(".")
            binding, tail = (
                (pieces[1], " ".join(pieces[2:]) or "value") if len(pieces) > 1 else ("", "value")
            )
            if binding:
                return ExplanationValue(
                    kind="loop_ref" if binding == loop_name else "binding_ref",
                    text=f"each {binding}'s {tail}"
                    if binding == loop_name
                    else f"{binding}'s {tail}",
                    raw=raw,
                )
        return ExplanationValue(kind="unresolved", text=raw, raw=raw)
    parts, cursor = [], 0
    for match in matches:
        if literal := raw[cursor : match.start()]:
            parts.append(json.dumps(literal, ensure_ascii=False))
        reference = match.group(1).strip()
        parts.append(
            _event_value(reference, event_type).text
            if reference.startswith("event.")
            else _value("{{" + reference + "}}", event_type=event_type, loop_name=loop_name).text
        )
        cursor = match.end()
    if literal := raw[cursor:]:
        parts.append(json.dumps(literal, ensure_ascii=False))
    return ExplanationValue(kind="template", text=" + ".join(parts), raw=raw)


def render_node_explanation(
    node_id: str,
    node: Mapping[str, Any],
    *,
    event_type: str | None = None,
    registry: ContractRegistry = CONTRACTS,
    node_labels: Mapping[str, str] | None = None,
) -> NodeExplanation | None:
    """Explain a compiled node without invoking a handler or resolving data."""
    try:
        action = node.get("action") if isinstance(node.get("action"), Mapping) else node
        if node.get("terminal") or not isinstance(action, Mapping):
            return None
        command = action.get("command")
        if not isinstance(command, str) or registry.get(command) is None:
            return None
        contract = registry.require(command).contract
        raw_args = action.get("args") if isinstance(action.get("args"), Mapping) else {}
        loop = (
            action.get("for_each")
            if isinstance(action.get("for_each"), Mapping)
            else node.get("for_each")
        )
        loop_name = (
            loop.get("as")
            if isinstance(loop, Mapping) and isinstance(loop.get("as"), str)
            else None
        )
        safe_args, inputs, unrendered = redact_args(contract, raw_args), [], []
        for field, raw in raw_args.items():
            if field not in contract.execution.args_model.model_fields:
                unrendered.append(str(field))
                continue
            value = _value(safe_args.get(field), event_type=event_type, loop_name=loop_name)
            source = (
                raw
                if isinstance(raw, str)
                else raw[0]
                if isinstance(raw, list) and len(raw) == 1
                else None
            )
            event_match = _EVENT_REF.fullmatch(source) if isinstance(source, str) else None
            if field in contract.execution.sensitive_args or (
                event_match and event_field_is_sensitive(event_type or "", event_match.group(1))
            ):
                value = ExplanationValue(kind=value.kind, text=REDACTED, raw=None, redacted=True)
            inputs.append(
                ExplanationInput(
                    field=field,
                    label=contract.presentation.arg_labels.get(
                        field, field.replace("_", " ").title()
                    ),
                    value=value,
                    required=contract.execution.args_model.model_fields[field].is_required(),
                )
            )
        labels, outcomes = node_labels or {}, []
        for key, label, classification in (
            ("on_success", "Success", "success"),
            ("on_failure", "Failure", "failure"),
        ):
            if isinstance(target := action.get(key), str):
                outcomes.append(
                    ExplanationOutcome(
                        outcome=classification,
                        label=label,
                        classification=classification,
                        target_node_id=target,
                        target_label=labels.get(target, target),
                    )
                )
        output = (
            action.get("output")
            if isinstance(action.get("output"), Mapping)
            else node.get("output")
        )
        result = (
            ExplanationResultBinding(
                name=output["as"], fields=list(contract.execution.result_model.model_fields)
            )
            if isinstance(output, Mapping) and isinstance(output.get("as"), str)
            else None
        )
        loop_info = (
            ExplanationLoop(
                source_text=str(loop.get("in", "each item")),
                item_binding=loop_name,
                source_raw=str(loop.get("in")),
            )
            if loop_name and isinstance(loop, Mapping)
            else None
        )
        idem = contract.execution.idempotency
        return NodeExplanation(
            kind="command",
            title=contract.presentation.title,
            command=command,
            contract_fingerprint=contract.fingerprint(),
            capability=contract.execution.capability,
            effects=[
                render_effect(c, raw_args, contract.presentation)
                for c in contract.execution.effects
            ],
            inputs=inputs,
            result=result,
            outcomes=outcomes,
            loop=loop_info,
            idempotency=f"Repeating with the same {idem.key_field} reuses the existing result"
            if idem.mode == "keyed"
            else "Repeating this operation is naturally idempotent"
            if idem.mode == "natural"
            else None,
            retry="Safe to retry" if contract.execution.retry_safe else "Not safe to retry",
            unrendered_fields=unrendered,
        )
    except Exception:
        logger.warning("graph-view: could not render intent for node %s", node_id, exc_info=True)
        return None
