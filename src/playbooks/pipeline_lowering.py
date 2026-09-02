"""Deterministic, deliberately narrow lowering of the two V1 machine graphs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.playbooks.authoring import PlaybookSource
from src.playbooks.proposal import CompileProposal, propose
from src.playbooks.validation import ContractLookup, Diagnostic, EventSchemaLookup, ProfileLookup

_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_INTERPOLATION = re.compile(r"\{\{(event|outputs)\.([A-Za-z_][\w.]*)\}\}")


@dataclass(frozen=True)
class ShadowRow:
    playbook_id: str
    vault_path: str
    kind: str
    lowered: bool
    error_count: int
    warning_count: int
    question_count: int
    diagnostics: list[Diagnostic]
    artifact_sha256: str | None


@dataclass(frozen=True)
class ShadowReport:
    rows: list[ShadowRow]


def _ref(source: PlaybookSource) -> dict[str, Any]:
    return {
        "path": source.vault_path,
        "start_line": source.body_start_line,
        "end_line": source.body_start_line,
    }


def _value(value: Any, loops: set[str] = frozenset()) -> Any:
    if isinstance(value, list):
        return {"type": "list", "items": [_value(item, loops) for item in value]}
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {key: _value(item, loops) for key, item in value.items()},
        }
    if not isinstance(value, str):
        return {"type": "literal", "value": value}
    whole = _INTERPOLATION.fullmatch(value)
    if whole:
        namespace, path = whole.groups()
        if namespace == "event":
            return {"type": "event_ref", "path": path}
        binding, _, rest = path.partition(".")
        return {
            "type": "loop_ref" if binding in loops else "binding_ref",
            "binding": binding,
            **({"path": rest} if rest else {}),
        }
    parts: list[Any] = []
    at = 0
    for match in _INTERPOLATION.finditer(value):
        if match.start() > at:
            parts.append({"type": "literal", "value": value[at : match.start()]})
        parts.append(_value(match.group(0), loops))
        at = match.end()
    if not parts:
        return {"type": "literal", "value": value}
    if at < len(value):
        parts.append({"type": "literal", "value": value[at:]})
    return {"type": "template", "parts": parts}


def _condition(raw: Any) -> Any | None:
    if not isinstance(raw, Mapping):
        return None
    if "all" in raw or "any" in raw:
        name = "all" if "all" in raw else "any"
        values = [_condition(item) for item in raw[name]]
        return {
            "type": "bool",
            "op": "and" if name == "all" else "or",
            "operands": [item for item in values if item],
        }
    field = raw.get("field")
    if not isinstance(field, str):
        return None
    event = {"type": "event_ref", "path": field.removeprefix("event.")}
    if "truthy" in raw:
        exists = {"type": "exists", "value": event, "mode": "truthy"}
        return exists if raw["truthy"] else {"type": "bool", "op": "not", "operands": [exists]}
    if "not_null" in raw:
        exists = {"type": "exists", "value": event, "mode": "present"}
        return exists if raw["not_null"] else {"type": "bool", "op": "not", "operands": [exists]}
    if "is_null" in raw:
        return {
            "type": "bool",
            "op": "not",
            "operands": [{"type": "exists", "value": event, "mode": "present"}],
        }
    if "equals" in raw:
        return {"type": "comparison", "op": "eq", "left": event, "right": _value(raw["equals"])}
    return None


def lower_pipeline(source: PlaybookSource) -> tuple[Mapping[str, Any], list[Diagnostic]]:
    match = _BLOCK.search(source.body)
    if not match:
        return {}, [
            Diagnostic(
                "question",
                "requires_agent_proposal",
                "pipeline has no machine JSON block",
                source=_source_ref(source),
            )
        ]
    try:
        graph = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return {}, [
            Diagnostic(
                "error",
                "ambiguous_prose",
                f"invalid pipeline JSON: {exc}",
                source=_source_ref(source),
            )
        ]
    rules: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}
    for raw_rule in graph.get("rules", []):
        rule_id = raw_rule.get("id")
        if not isinstance(rule_id, str):
            continue
        nodes = raw_rule.get("nodes", {})
        # V1 scopes node ids to a rule while V2's steps map is global.  The
        # deterministic prefix is artifact-local and therefore does not need
        # an author inventory declaration.
        def node_key(node_id: str) -> str:
            return f"{rule_id}--{node_id}"
        for node_id, node in nodes.items():
            step_id = node_key(node_id)
            if node.get("terminal"):
                steps[step_id] = {
                    "type": "terminal",
                    "rule": rule_id,
                    "title": node_id,
                    "source": _ref(source),
                    "outcome": "completed",
                }
                continue
            loop = node.get("for_each") or {}
            loop_name = loop.get("as")
            base_id = f"{step_id}-body" if loop_name else step_id
            transitions = {
                "success": node_key(node.get("on_success", "done")),
                "runtime_error": node_key(node.get("on_failure", node.get("on_success", "done"))),
            }
            command = {
                "type": "command",
                "rule": rule_id,
                "title": node_id,
                "source": _ref(source),
                "command": node.get("command", "unknown"),
                "inputs": {
                    key: _value(value, {loop_name} if loop_name else set())
                    for key, value in (node.get("args") or {}).items()
                },
                "transitions": transitions,
            }
            output = node.get("output") or {}
            if output.get("as"):
                command["save_result_as"] = output["as"]
            if loop_name:
                item_done = f"{step_id}-item-done"
                command["transitions"] = {"success": item_done, "runtime_error": item_done}
                steps[base_id] = command
                steps[item_done] = {
                    "type": "terminal",
                    "rule": rule_id,
                    "title": f"{node_id} iteration complete",
                    "source": _ref(source),
                    "outcome": "completed",
                }
                steps[step_id] = {
                    "type": "foreach",
                    "rule": rule_id,
                    "title": node_id,
                    "source": _ref(source),
                    "collection": _value(loop.get("source")),
                    "item_binding": loop_name,
                    "failure_policy": "collect",
                    "body_entry": base_id,
                    "continuation": node_key(node.get("on_success", "done")),
                    "transitions": {
                        "completed": node_key(node.get("on_success", "done")),
                        "failed": node_key(node.get("on_failure", node.get("on_success", "done"))),
                    },
                }
            else:
                steps[step_id] = command
        event = raw_rule.get("on", "")
        rules.append(
            {
                "id": rule_id,
                "name": rule_id,
                "trigger": {"event_type": event},
                "guard": _condition(raw_rule.get("when")),
                "entry_step": node_key(raw_rule.get("entry", "")),
                "source": _ref(source),
            }
        )
    return {"rules": rules, "steps": steps}, []


def _source_ref(source: PlaybookSource):
    from src.playbooks.definition import SourceRef

    return SourceRef(**_ref(source))


def lower_assignment(source: PlaybookSource) -> tuple[Mapping[str, Any], list[Diagnostic]]:
    return {}, [
        Diagnostic(
            "question",
            "requires_agent_proposal",
            "assignment-routing prose requires an explicit AI profile and budget",
            source=_source_ref(source),
        )
    ]


def shadow_compile(
    sources: Iterable[PlaybookSource],
    *,
    contracts: ContractLookup,
    profiles: ProfileLookup,
    events: EventSchemaLookup,
) -> ShadowReport:
    rows: list[ShadowRow] = []
    for source in sources:
        kind = str(source.frontmatter.get("kind") or "")
        if kind == "pipeline":
            body, diagnostics = lower_pipeline(source)
        elif kind == "assignment-routing":
            body, diagnostics = lower_assignment(source)
        else:
            body, diagnostics = (
                {},
                [
                    Diagnostic(
                        "question",
                        "requires_agent_proposal",
                        "prose playbook requires a compiler-agent proposal",
                        source=_source_ref(source),
                    )
                ],
            )
        proposal: CompileProposal | None = None
        if body:
            proposal = propose(
                source, body, contracts=contracts, profiles=profiles, events=events, version=1
            )
            diagnostics += proposal.diagnostics
        counts = {
            severity: sum(d.severity == severity for d in diagnostics)
            for severity in ("error", "warning", "question")
        }
        rows.append(
            ShadowRow(
                str(source.frontmatter.get("id", "")),
                source.vault_path,
                kind,
                bool(body),
                counts["error"],
                counts["warning"],
                counts["question"],
                diagnostics,
                proposal.artifact_sha256 if proposal and not counts["error"] else None,
            )
        )
    return ShadowReport(rows)
