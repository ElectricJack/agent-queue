"""Structural diff for reviewable V2 proposals; it never mutates an artifact."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.playbooks.definition import PlaybookDefinition, is_executable_path, step_targets

DiffChange = Literal["added", "removed", "modified", "unchanged"]


@dataclass(frozen=True)
class FieldChange:
    pointer: str
    before: Any | None
    after: Any | None
    executable: bool


@dataclass(frozen=True)
class StepChange:
    step_id: str
    rule_id: str | None
    change: DiffChange
    fields: list[FieldChange]


@dataclass(frozen=True)
class RuleChange:
    rule_id: str
    change: DiffChange
    steps_added: list[str]
    steps_removed: list[str]


@dataclass(frozen=True)
class EdgeChange:
    edge_id: str
    rule_id: str
    source: str
    target: str
    outcome: str
    change: DiffChange


@dataclass(frozen=True)
class DefinitionDiff:
    rules: list[RuleChange]
    steps: list[StepChange]
    edges: list[EdgeChange]
    contracts: list[tuple[str, str | None, str | None]]
    executable_change: bool
    semantic_change_count: int
    presentation_change_count: int


def _flat(value: Any, pointer: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            out.update(_flat(child, f"{pointer}/{key}"))
        return out
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for index, child in enumerate(value):
            out.update(_flat(child, f"{pointer}/{index}"))
        return out
    return {pointer: value}


def _edges(definition: PlaybookDefinition | None) -> dict[str, tuple[str, str, str, str]]:
    result: dict[str, tuple[str, str, str, str]] = {}
    if definition is None:
        return result
    for source, step in definition.steps.items():
        for pointer, target in step_targets(step).items():
            outcome = pointer.rsplit("/", 1)[-1]
            key = f"{step.rule}::{source}::{outcome}"
            result[key] = (step.rule, source, target, outcome)
    return result


def diff_definitions(base: PlaybookDefinition | None, target: PlaybookDefinition) -> DefinitionDiff:
    before_steps = {} if base is None else base.steps
    step_changes: list[StepChange] = []
    exec_count = present_count = 0
    for step_id in sorted(set(before_steps) | set(target.steps)):
        before, after = before_steps.get(step_id), target.steps.get(step_id)
        rule = (after or before).rule
        if before is None or after is None:
            change: DiffChange = "added" if after else "removed"
            fields = [FieldChange("", before, after, True)]
        else:
            left, right = (
                _flat(before.model_dump(mode="json", exclude_none=True)),
                _flat(after.model_dump(mode="json", exclude_none=True)),
            )
            fields = [
                FieldChange(
                    pointer,
                    left.get(pointer),
                    right.get(pointer),
                    is_executable_path(f"/steps/{step_id}{pointer}"),
                )
                for pointer in sorted(set(left) | set(right))
                if left.get(pointer) != right.get(pointer)
            ]
            change = "modified" if fields else "unchanged"
        for field in fields:
            if field.executable:
                exec_count += 1
            else:
                present_count += 1
        step_changes.append(StepChange(step_id, rule, change, fields))
    before_rules = {} if base is None else {rule.id: rule for rule in base.rules}
    after_rules = {rule.id: rule for rule in target.rules}
    rules = [
        RuleChange(
            rule_id,
            "added"
            if rule_id not in before_rules
            else "removed"
            if rule_id not in after_rules
            else "unchanged",
            [
                sid
                for sid, step in target.steps.items()
                if step.rule == rule_id and sid not in before_steps
            ],
            [
                sid
                for sid, step in before_steps.items()
                if step.rule == rule_id and sid not in target.steps
            ],
        )
        for rule_id in sorted(set(before_rules) | set(after_rules))
    ]
    old_edges, new_edges = _edges(base), _edges(target)
    edges = [
        EdgeChange(
            key,
            *(new_edges.get(key) or old_edges[key]),
            "added" if key not in old_edges else "removed" if key not in new_edges else "unchanged",
        )
        for key in sorted(set(old_edges) | set(new_edges))
    ]
    old_contracts = {} if base is None else base.compiled_against.commands
    contracts = [
        (name, old_contracts.get(name), target.compiled_against.commands.get(name))
        for name in sorted(set(old_contracts) | set(target.compiled_against.commands))
        if old_contracts.get(name) != target.compiled_against.commands.get(name)
    ]
    exec_count += len(contracts) + sum(change.change != "unchanged" for change in edges)
    return DefinitionDiff(
        rules,
        step_changes,
        edges,
        contracts,
        bool(exec_count),
        exec_count + present_count,
        present_count,
    )
