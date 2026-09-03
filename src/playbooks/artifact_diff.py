"""Semantic, execution-aware diffs for immutable Playbook V2 artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.api.models.playbook_v2 import PlaybookArtifactDiffResponse
from src.playbooks.definition import PlaybookDefinition
from src.playbooks.graph_projection import project_edges, project_graph, project_value


_PRESENTATION_ROOTS = frozenset({"title", "description", "source"})


def _plain_ref(ref: Any) -> dict[str, Any] | None:
    if ref is None:
        return None
    if hasattr(ref, "as_dict"):
        return ref.as_dict()
    return dict(ref)


def _fields(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        rows = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{key}"
            if key not in before:
                rows.append(_field(child, None, after[key]))
            elif key not in after:
                rows.append(_field(child, before[key], None))
            else:
                rows.extend(_fields(before[key], after[key], child))
        return rows
    if isinstance(before, list) and isinstance(after, list):
        rows = []
        for index in range(max(len(before), len(after))):
            child = f"{path}/{index}"
            if index >= len(before):
                rows.append(_field(child, None, after[index]))
            elif index >= len(after):
                rows.append(_field(child, before[index], None))
            else:
                rows.extend(_fields(before[index], after[index], child))
        return rows
    return [_field(path or "/", before, after)]


def _field(path: str, before: Any, after: Any) -> dict[str, Any]:
    parts = [part for part in path.split("/") if part]
    executable = not bool(parts and (parts[0] in _PRESENTATION_ROOTS or parts[-1] == "label"))
    return {
        "path": path,
        "before": project_value({"type": "literal", "value": before}) if before is not None else None,
        "after": project_value({"type": "literal", "value": after}) if after is not None else None,
        "executable": executable,
    }


def _node_explanations(
    definition: PlaybookDefinition | None,
    ref: Any,
    contracts: Any,
    profiles: Any,
) -> dict[str, dict]:
    if definition is None:
        return {}
    activation = {
        "playbook_id": definition.id,
        "scope": definition.scope.type,
        "enabled": False,
        "health": "disabled",
    }
    graph = project_graph(definition, ref, activation, contracts=contracts, profiles=profiles)
    return {node["id"]: node["explanation"] for node in graph["nodes"]}


def _rule_rows(base: PlaybookDefinition | None, target: PlaybookDefinition) -> list[dict]:
    old = {rule.id: rule for rule in base.rules} if base else {}
    new = {rule.id: rule for rule in target.rules}
    rows = []
    for rule_id in sorted(set(old) | set(new)):
        before, after = old.get(rule_id), new.get(rule_id)
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        elif before.model_dump(mode="json") == after.model_dump(mode="json"):
            change = "unchanged"
        else:
            change = "modified"
        old_steps = {key for key, value in base.steps.items() if value.rule == rule_id} if base else set()
        new_steps = {key for key, value in target.steps.items() if value.rule == rule_id}
        rows.append(
            {
                "rule_id": rule_id,
                "change": change,
                "event_type_before": before.trigger.event_type if before else None,
                "event_type_after": after.trigger.event_type if after else None,
                "step_ids_added": sorted(new_steps - old_steps),
                "step_ids_removed": sorted(old_steps - new_steps),
            }
        )
    return rows


def _edge_rows(base: PlaybookDefinition | None, target: PlaybookDefinition) -> list[dict]:
    old = {edge["id"]: edge for edge in project_edges(base)} if base else {}
    new = {edge["id"]: edge for edge in project_edges(target)}
    rows = []
    for edge_id in sorted(set(old) | set(new)):
        before, after = old.get(edge_id), new.get(edge_id)
        edge = after or before
        assert edge is not None
        change = "added" if before is None else "removed" if after is None else "unchanged"
        if before is not None and after is not None and before != after:
            change = "modified"
        rows.append(
            {
                "edge_id": edge_id,
                "rule_id": edge["rule_id"],
                "source": edge["source"],
                "target": edge["target"],
                "outcome": edge["outcome"],
                "change": change,
            }
        )
    return rows


def _contract_rows(base: PlaybookDefinition | None, target: PlaybookDefinition) -> list[dict]:
    old = dict(base.compiled_against.commands) if base else {}
    new = dict(target.compiled_against.commands)
    rows = []
    for command in sorted(set(old) | set(new)):
        before, after = old.get(command), new.get(command)
        change = "added" if before is None else "removed" if after is None else "unchanged"
        if before is not None and after is not None and before != after:
            change = "modified"
        rows.append(
            {
                "command": command,
                "fingerprint_before": before,
                "fingerprint_after": after,
                "change": change,
            }
        )
    return rows


def diff_artifacts(
    base: PlaybookDefinition | None,
    target: PlaybookDefinition,
    *,
    base_ref: Any | None,
    target_ref: Any,
    contracts: Any = None,
    profiles: Any = None,
) -> dict[str, Any]:
    """Diff semantic model fields, ignoring serialization and mapping order."""
    base = PlaybookDefinition.model_validate(base) if base is not None else None
    target = PlaybookDefinition.model_validate(target)
    before_explanations = _node_explanations(base, base_ref, contracts, profiles)
    after_explanations = _node_explanations(target, target_ref, contracts, profiles)
    old_steps = base.steps if base else {}
    new_steps = target.steps
    step_rows = []
    semantic_count = 0
    presentation_count = 0
    for step_id in sorted(set(old_steps) | set(new_steps)):
        before, after = old_steps.get(step_id), new_steps.get(step_id)
        if before is None:
            changes = [_field("/", None, after.model_dump(mode="json"))]
            change = "added"
        elif after is None:
            changes = [_field("/", before.model_dump(mode="json"), None)]
            change = "removed"
        else:
            changes = _fields(before.model_dump(mode="json"), after.model_dump(mode="json"))
            change = "modified" if changes else "unchanged"
        semantic_count += sum(row["executable"] for row in changes)
        presentation_count += sum(not row["executable"] for row in changes)
        item = after or before
        assert item is not None
        step_rows.append(
            {
                "step_id": step_id,
                "rule_id": item.rule,
                "change": change,
                "step_kind": item.type,
                "title_before": before.title if before else None,
                "title_after": after.title if after else None,
                "field_changes": changes,
                "explanation_before": before_explanations.get(step_id),
                "explanation_after": after_explanations.get(step_id),
            }
        )
    rules = _rule_rows(base, target)
    edges = _edge_rows(base, target)
    contracts_changed = _contract_rows(base, target)
    if base is not None:
        old_rules = {rule.id: rule.model_dump(mode="json") for rule in base.rules}
        new_rules = {rule.id: rule.model_dump(mode="json") for rule in target.rules}
        for rule_id in set(old_rules) & set(new_rules):
            rule_changes = _fields(old_rules[rule_id], new_rules[rule_id])
            semantic_count += sum(row["executable"] for row in rule_changes)
            presentation_count += sum(not row["executable"] for row in rule_changes)
    # Structural graph and contract changes are executable even if no step field changed.
    semantic_count += sum(row["change"] != "unchanged" for row in edges)
    semantic_count += sum(row["change"] != "unchanged" for row in contracts_changed)
    if base is None:
        semantic_count = max(semantic_count, 1)
    diagnostics = []
    for command, expected in target.compiled_against.commands.items():
        found = contracts.get(command) if contracts is not None else None
        actual = getattr(found, "execution_fingerprint", None)
        if found is None:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "unknown_command",
                    "message": f"Command {command!r} is not registered",
                    "rule_id": None,
                    "step_id": None,
                    "source": None,
                }
            )
        elif actual and actual != expected:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "stale_contract",
                    "message": f"Command contract changed for {command!r}",
                    "rule_id": None,
                    "step_id": None,
                    "source": None,
                }
            )
    blockers = [item["message"] for item in diagnostics]
    response = {
        "success": True,
        "base": _plain_ref(base_ref),
        "target": _plain_ref(target_ref),
        "executable_change": semantic_count > 0,
        "semantic_change_count": semantic_count,
        "presentation_change_count": presentation_count,
        "rules": rules,
        "steps": step_rows,
        "edges": edges,
        "contracts": contracts_changed,
        "diagnostics": diagnostics,
        "activation_blocked": bool(blockers),
        "activation_blockers": blockers,
    }
    return PlaybookArtifactDiffResponse.model_validate(response).model_dump(mode="json")


__all__ = ["diff_artifacts"]
