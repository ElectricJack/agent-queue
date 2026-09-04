"""Architectural ratchets for the forward-only Playbooks V2 runtime."""

from __future__ import annotations

import ast
from pathlib import Path

from src.config import PlaybooksConfig
from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES


ROOT = Path(__file__).parents[1]
V1_MODULES = {
    "src.playbooks.assignment_compiler",
    "src.playbooks.conditions",
    "src.playbooks.cutover",
    "src.playbooks.cutover_window",
    "src.playbooks.manager",
    "src.playbooks.pipeline_compiler",
    "src.playbooks.pipeline_runner",
    "src.playbooks.runner",
    "src.playbooks.runner_context",
    "src.playbooks.runner_events",
    "src.playbooks.runner_transitions",
    "src.playbooks.store",
    "src.playbooks.token_tracker",
    "src.commands.playbook_cutover_commands",
    "src.commands.playbook_migration_commands",
}


def test_v1_modules_are_deleted() -> None:
    for module in sorted(V1_MODULES):
        assert not (ROOT / (module.replace(".", "/") + ".py")).exists(), module


def test_source_does_not_import_v1_modules() -> None:
    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name in V1_MODULES:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    assert violations == []


def test_playbook_config_has_no_runtime_selector_or_v1_admission() -> None:
    fields = PlaybooksConfig.__dataclass_fields__
    retired = {
        "v1_admission",
        "v2_engine",
        "v2_api",
        "v2_activation_writes",
        "v2_compiler_enabled",
        "v2_storage_enabled",
    }
    assert retired.isdisjoint(fields)


def test_no_cutover_or_migration_tools_exist() -> None:
    names = set(_TOOL_CATEGORIES)
    assert not any("cutover" in name for name in names)
    assert not any("v1_admission" in name for name in names)
    assert not any("migration_ack" in name for name in names)
    assert not any("migration_unack" in name for name in names)


def test_activation_has_no_core_review_acknowledgement_gate() -> None:
    activation = next(
        tool for tool in _ALL_TOOL_DEFINITIONS
        if tool["name"] == "playbook_activate"
    )
    properties = activation["input_schema"]["properties"]
    assert "acknowledge_diff" not in properties
    assert "reviewed_by" not in properties
    assert "reviewed_at" not in properties
