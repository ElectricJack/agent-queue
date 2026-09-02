"""Pure, contract-derived explanations for current playbook nodes."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, assert_never

from src.api.models.playbook import (
    ExplanationEffect,
    ExplanationInput,
    ExplanationLoop,
    ExplanationOutcome,
    ExplanationResultBinding,
    ExplanationValue,
    NodeExplanation,
)
from src.commands.contracts.models import (
    REDACTED,
    CommandContract,
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


def canonical_effect(contract: CommandContract[Any, Any]) -> ExplanationEffect:
    """The lossless fallback for a contract that declares no effect clause.

    Design spec :360/:389 — "a lossless canonical field/value rendering is
    always available when no richer clause applies"; the renderer never hides
    a command's effect and never invents one.  Built from the two things the
    execution contract always has: its side-effect class and its argument
    names.
    """
    execution = contract.execution
    fields = ", ".join(execution.args_model.model_fields) or "no arguments"
    return ExplanationEffect(
        operation=execution.side_effect.value,
        text=f"{execution.side_effect.value.replace('_', ' ').capitalize()} using {fields}",
        condition=None,
        subject=None,
    )


def _arg_label(field: str, presentation: CommandPresentation) -> str:
    """The operator-facing name of an argument, never the bare field name."""
    return presentation.arg_labels.get(field) or field.replace("_", " ").capitalize()


def _idempotency_text(contract: CommandContract[Any, Any]) -> str:
    """One sentence about repeating the call, derived from the contract.

    ``keyed`` names the key by its presentation label and the reused object by
    the subject of the contract's first effect clause, so the sentence moves
    with the contract rather than being authored twice.
    """
    execution = contract.execution
    mode = execution.idempotency.mode
    if mode == "natural":
        return "Repeating this operation is naturally idempotent"
    if mode == "none":
        return "Repeating this repeats the effect"
    key = execution.idempotency.key_field or ""
    label = contract.presentation.arg_labels.get(key)
    key_text = label[0].lower() + label[1:] if label else key
    subject = (
        execution.effects[0].subject.value.replace("_", " ") if execution.effects else "result"
    )
    return f"Repeating with the same {key_text} reuses the existing {subject}"


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
                    label=_arg_label(field, contract.presentation),
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
        # ``source`` is the executable key the pipeline runner reads
        # (``PipelineRunner._run_for_each``); the rendered line must name the
        # same expression the loop actually iterates, minus the ``outputs.``
        # scope prefix that is noise to a reader.
        loop_source = loop.get("source") if isinstance(loop, Mapping) else None
        loop_info = (
            ExplanationLoop(
                source_text=f"each item in {loop_source.removeprefix('outputs.')}"
                if isinstance(loop_source, str) and loop_source
                else "each item",
                item_binding=loop_name,
                source_raw=loop_source if isinstance(loop_source, str) else None,
            )
            if loop_name and isinstance(loop, Mapping)
            else None
        )
        return NodeExplanation(
            kind="command",
            title=contract.presentation.title,
            command=command,
            contract_fingerprint=contract.fingerprint(),
            capability=contract.execution.capability,
            effects=[
                render_effect(c, raw_args, contract.presentation)
                for c in contract.execution.effects
            ]
            or [canonical_effect(contract)],
            inputs=inputs,
            result=result,
            outcomes=outcomes,
            loop=loop_info,
            idempotency=_idempotency_text(contract),
            retry="Safe to retry" if contract.execution.retry_safe else "Not safe to retry",
            unrendered_fields=unrendered,
        )
    except Exception:
        logger.warning("graph-view: could not render intent for node %s", node_id, exc_info=True)
        return None
