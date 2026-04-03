"""Meta-tests to ensure all tools have test coverage and no typos exist.

Updated: imports from src.supervisor instead of src.chat_agent (post-supervisor refactor).
"""

from __future__ import annotations

from src.supervisor import TOOLS
from tests.chat_eval.test_cases._loader import load_all_cases


def _all_tool_names() -> set[str]:
    """Extract all tool names from the TOOLS constant in supervisor.py."""
    return {tool["name"] for tool in TOOLS}


def _all_tested_tool_names() -> set[str]:
    """Extract all tool names referenced in test cases (expected + not_expected)."""
    names: set[str] = set()
    for case in load_all_cases():
        for turn in case.turns:
            for expected in turn.expected_tools:
                names.add(expected.name)
            for forbidden in turn.not_expected_tools:
                names.add(forbidden)
    return names


def test_all_tools_have_test_cases():
    """Every tool in the TOOLS list should appear in at least one test case."""
    all_tools = _all_tool_names()
    tested_tools = _all_tested_tool_names()

    untested = all_tools - tested_tools
    assert not untested, f"{len(untested)} tool(s) have no test cases:\n" + "\n".join(
        f"  - {name}" for name in sorted(untested)
    )


def test_no_test_cases_reference_nonexistent_tools():
    """No test case should reference a tool name that doesn't exist in TOOLS."""
    all_tools = _all_tool_names()
    tested_tools = _all_tested_tool_names()

    nonexistent = tested_tools - all_tools
    assert not nonexistent, (
        f"{len(nonexistent)} tool name(s) in test cases don't exist in TOOLS:\n"
        + "\n".join(f"  - {name}" for name in sorted(nonexistent))
    )


def test_no_duplicate_case_ids():
    """All test case IDs should be unique."""
    cases = load_all_cases()
    ids = [c.id for c in cases]
    duplicates = [id_ for id_ in ids if ids.count(id_) > 1]
    assert not duplicates, f"Duplicate test case IDs found:\n" + "\n".join(
        f"  - {id_}" for id_ in sorted(set(duplicates))
    )


def test_minimum_case_count():
    """We should have at least 200 test cases to provide meaningful coverage."""
    cases = load_all_cases()
    assert len(cases) >= 200, (
        f"Only {len(cases)} test cases — expected at least 200 for comprehensive coverage"
    )
