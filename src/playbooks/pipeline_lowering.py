"""Deterministic, deliberately narrow lowering of the two V1 machine graphs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.playbooks.authoring import PlaybookSource
from src.playbooks.proposal import CompileProposal, propose
from src.playbooks.validation import (
    ContractLookup,
    Diagnostic,
    EventSchemaLookup,
    ProfileLookup,
    RegistryContractLookup,
)

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


def _ref(source: PlaybookSource, line: int | None = None, excerpt: str | None = None) -> dict[str, Any]:
    line = line or source.body_start_line
    return {
        "path": source.vault_path,
        "start_line": line,
        "end_line": line,
        **({"excerpt": excerpt.strip()} if excerpt and excerpt.strip() else {}),
    }


class _JsonKeyLines:
    """Exact 1-based source lines for keys inside the fenced JSON graph."""

    def __init__(self, source: PlaybookSource, match: re.Match[str]) -> None:
        self._source = source
        self._lines = match.group(1).splitlines()
        self._first_line = source.body_start_line + source.body[: match.start(1)].count("\n")

    def ref_for_pair(self, key: str, value: str) -> dict[str, Any]:
        pattern = re.compile(
            rf'"{re.escape(key)}"\s*:\s*"{re.escape(value)}"'
        )
        return self._find(pattern)

    def ref_for_object_key(self, key: str, *, start_line: int | None = None) -> dict[str, Any]:
        pattern = re.compile(rf'^\s*"{re.escape(key)}"\s*:\s*\{{')
        return self._find(pattern, start_line=start_line)

    def _find(
        self, pattern: re.Pattern[str], *, start_line: int | None = None
    ) -> dict[str, Any]:
        start_offset = max(0, (start_line or self._first_line) - self._first_line)
        for offset, text in enumerate(self._lines[start_offset:], start=start_offset):
            if pattern.search(text):
                return _ref(self._source, self._first_line + offset, text)
        return _ref(self._source)


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


def _bare_ref(value: Any) -> Any:
    """Lower a ``for_each.source``, which is a bare reference, not a template.

    V1 wrote the loop collection as ``outputs.downstream.tasks`` with no
    ``{{ }}`` around it (``pipeline_runner._resolve_ref`` reads it directly),
    so passing it through :func:`_value` produced a *literal string* and the
    lowered foreach iterated nothing a V1 run iterated.  Package 6's parity
    harness is what surfaced it: the loop events emitted ``gate_create`` under
    V1 and nothing under V2.
    """
    if not isinstance(value, str):
        return _value(value)
    namespace, _, path = value.partition(".")
    if not path:
        return _value(value)
    if namespace == "event":
        return {"type": "event_ref", "path": path}
    if namespace == "outputs":
        binding, _, rest = path.partition(".")
        return {
            "type": "binding_ref",
            "binding": binding,
            **({"path": rest} if rest else {}),
        }
    return _value(value)


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


def _command_transitions(
    command: str,
    *,
    success_target: str,
    failure_target: str,
    contracts: ContractLookup,
) -> dict[str, str]:
    """Expand V1's binary edges over the contract's closed outcome set."""
    contract = contracts.get(command)
    if contract is None:
        return {"runtime_error": failure_target}
    transitions: dict[str, str] = {}
    for outcome in sorted(contract.outcomes):
        classification = contract.outcome_classes.get(outcome)
        if classification is None:
            classification = (
                "failure" if outcome in {"failed", "failure", "rejected", "cancelled"} else "success"
            )
        transitions[outcome] = failure_target if classification == "failure" else success_target
    transitions["runtime_error"] = failure_target
    return transitions


def lower_pipeline(
    source: PlaybookSource,
    *,
    contracts: ContractLookup | None = None,
) -> tuple[Mapping[str, Any], list[Diagnostic]]:
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
    contracts = contracts or RegistryContractLookup()
    locations = _JsonKeyLines(source, match)
    rules: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}
    for raw_rule in graph.get("rules", []):
        rule_id = raw_rule.get("id")
        if not isinstance(rule_id, str):
            continue
        rule_source = locations.ref_for_pair("id", rule_id)
        nodes = raw_rule.get("nodes", {})
        # V1 scopes node ids to a rule while V2's steps map is global.  The
        # deterministic prefix is artifact-local and therefore does not need
        # an author inventory declaration.
        def node_key(node_id: str) -> str:
            return f"{rule_id}--{node_id}"
        for node_id, node in nodes.items():
            step_id = node_key(node_id)
            source_ref = locations.ref_for_object_key(
                node_id, start_line=rule_source["start_line"]
            )
            if node.get("terminal"):
                steps[step_id] = {
                    "type": "terminal",
                    "rule": rule_id,
                    "title": node_id,
                    "source": source_ref,
                    "outcome": "completed",
                }
                continue
            loop = node.get("for_each") or {}
            loop_name = loop.get("as")
            base_id = f"{step_id}-body" if loop_name else step_id
            success_target = node_key(node.get("on_success", "done"))
            failure_target = node_key(node.get("on_failure", node.get("on_success", "done")))
            transitions = _command_transitions(
                str(node.get("command", "unknown")),
                success_target=success_target,
                failure_target=failure_target,
                contracts=contracts,
            )
            command = {
                "type": "command",
                "rule": rule_id,
                "title": node_id,
                "source": source_ref,
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
                # Every body outcome re-enters the foreach node.  The foreach
                # executor owns iteration completion and is the only node that
                # may take the loop's external continuation/failure edges.
                command["transitions"] = {
                    outcome: step_id for outcome in command["transitions"]
                }
                steps[base_id] = command
                steps[step_id] = {
                    "type": "foreach",
                    "rule": rule_id,
                    "title": node_id,
                    "source": source_ref,
                    "collection": _bare_ref(loop.get("source")),
                    "item_binding": loop_name,
                    "failure_policy": "collect",
                    "body_entry": base_id,
                    "continuation": node_key(node.get("on_success", "done")),
                    "transitions": {
                        "completed": node_key(node.get("on_success", "done")),
                        "failed": node_key(node.get("on_failure", node.get("on_success", "done"))),
                        "runtime_error": node_key(
                            node.get("on_failure", node.get("on_success", "done"))
                        ),
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
                "source": rule_source,
            }
        )
    return {"rules": rules, "steps": steps}, []


def _source_ref(source: PlaybookSource):
    from src.playbooks.definition import SourceRef

    return SourceRef(**_ref(source))


def lower_assignment(source: PlaybookSource) -> tuple[Mapping[str, Any], list[Diagnostic]]:
    """Lower the fixed assignment router to one AI node plus its terminal."""
    rule_id = "assignment-route"
    choose = "assignment-route--choose"
    done = "assignment-route--done"
    max_tokens = int(source.frontmatter.get("max_tokens") or 4096)
    # ``role`` remains the V1 discriminator. V2 needs an independently
    # resolvable profile identity as well.
    profile_id = str(
        source.frontmatter.get("profile_id")
        or source.frontmatter.get("role")
        or "assignment-routing"
    )
    source_ref = _ref(source, source.body_start_line, source.body.strip().splitlines()[0])
    return {
        "rules": [
            {
                "id": rule_id,
                "name": "Assignment routing",
                "trigger": {"event_type": "assignment.route.requested"},
                "entry_step": choose,
                "source": source_ref,
            }
        ],
        "steps": {
            choose: {
                "type": "llm",
                "rule": rule_id,
                "title": "Choose assignment routes",
                "source": source_ref,
                "profile_id": profile_id,
                "prompt": {"type": "literal", "value": source.body.strip()},
                "inputs": {
                    "tasks": {"type": "event_ref", "path": "tasks"},
                    "options": {"type": "event_ref", "path": "options"},
                    "options_hash": {"type": "event_ref", "path": "options_hash"},
                    "catalog_hash": {"type": "event_ref", "path": "catalog_hash"},
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "decisions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "task_id": {"type": "string"},
                                    "input_hash": {"type": "string"},
                                    "intelligence_class": {"type": "string"},
                                    "provider": {"type": ["string", "null"]},
                                    "reason": {"type": "string", "minLength": 1, "maxLength": 400},
                                },
                                "required": [
                                    "task_id", "input_hash", "intelligence_class", "reason"
                                ],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["decisions"],
                    "additionalProperties": False,
                },
                "budget": {
                    "max_calls": 1,
                    "max_output_tokens": max_tokens,
                    "max_total_tokens": max_tokens,
                    "timeout_seconds": 300,
                },
                "save_result_as": "routing_result",
                "transitions": {"completed": done, "runtime_error": done},
            },
            done: {
                "type": "terminal",
                "rule": rule_id,
                "title": "Assignment routing complete",
                "source": source_ref,
                "outcome": "completed",
                "result": {"type": "binding_ref", "binding": "routing_result"},
            },
        },
    }, []


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
            body, diagnostics = lower_pipeline(source, contracts=contracts)
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
                source,
                body,
                contracts=contracts,
                profiles=profiles,
                events=events,
                version=1,
                # Machine-compiled JSON is executable source, not an agent
                # proposal inventing names absent from prose backticks.
                enforce_inventory=False,
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
