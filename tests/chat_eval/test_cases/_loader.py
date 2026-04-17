"""Discovery and loading of test cases from category modules."""

from __future__ import annotations

import importlib
from typing import Sequence

from tests.chat_eval.test_cases._types import Difficulty, TestCase

# Category module names (each exports a CASES list)
CATEGORY_MODULES = [
    "projects",
    "tasks",
    "task_workflow",
    "dependencies",
    "agents",
    "workspaces",
    "git",
    "notes",
    "system",
    "files",
    "archive",
    "ambiguous",
    "multi_step",
    "error_handling",
    "context_dependent",
    "memory",
    "profiles",
    "rules",
]


def load_all_cases() -> list[TestCase]:
    """Import all category modules and collect their CASES lists."""
    all_cases: list[TestCase] = []
    for module_name in CATEGORY_MODULES:
        mod = importlib.import_module(f"tests.chat_eval.test_cases.{module_name}")
        cases = getattr(mod, "CASES", [])
        all_cases.extend(cases)
    return all_cases


def by_category(cases: Sequence[TestCase], category: str) -> list[TestCase]:
    """Filter cases by category name."""
    return [c for c in cases if c.category == category]


def by_tag(cases: Sequence[TestCase], tag: str) -> list[TestCase]:
    """Filter cases that have a specific tag."""
    return [c for c in cases if tag in c.tags]


def by_difficulty(cases: Sequence[TestCase], difficulty: Difficulty) -> list[TestCase]:
    """Filter cases by difficulty level."""
    return [c for c in cases if c.difficulty == difficulty]


def case_ids(cases: Sequence[TestCase]) -> list[str]:
    """Extract test case IDs for pytest.mark.parametrize."""
    return [c.id for c in cases]
