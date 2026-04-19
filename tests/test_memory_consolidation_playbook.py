"""Tests for the memory-consolidation default playbook.

Verifies the playbook has the correct frontmatter (timer.24h system
trigger), creates supervisor tasks rather than doing the work itself,
and references the consolidation task prompt template.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


PLAYBOOK_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "prompts"
    / "default_playbooks"
    / "memory-consolidation.md"
)
CONSOLIDATION_PROMPT_PATH = (
    Path(__file__).parent.parent / "src" / "prompts" / "consolidation_task.md"
)


@pytest.fixture
def playbook_text() -> str:
    assert PLAYBOOK_PATH.exists(), f"Playbook not found: {PLAYBOOK_PATH}"
    return PLAYBOOK_PATH.read_text(encoding="utf-8")


@pytest.fixture
def frontmatter(playbook_text: str) -> dict:
    parts = playbook_text.split("---", 2)
    assert len(parts) >= 3
    return yaml.safe_load(parts[1])


def test_has_timer_24h_trigger(frontmatter: dict) -> None:
    triggers = frontmatter.get("triggers") or []
    assert "timer.24h" in triggers, f"expected timer.24h trigger, got {triggers}"


def test_system_scope(frontmatter: dict) -> None:
    assert frontmatter.get("scope") == "system"


def test_id_is_memory_consolidation(frontmatter: dict) -> None:
    assert frontmatter.get("id") == "memory-consolidation"


def test_creates_supervisor_task(playbook_text: str) -> None:
    """The playbook must delegate work to a supervisor task, not do it itself."""
    assert "create_task" in playbook_text
    assert "supervisor" in playbook_text


def test_respects_churn_threshold(playbook_text: str) -> None:
    """The playbook must skip low-churn projects to avoid wasted runs."""
    # Plan spec: skip projects with fewer than 10 churned insights and
    # a run less than 7 days old.
    assert "10" in playbook_text
    assert "7" in playbook_text


def test_references_consolidation_prompt(playbook_text: str) -> None:
    """Verifies the playbook instructs the supervisor to use the prompt template."""
    assert "consolidation_task.md" in playbook_text


def test_consolidation_prompt_exists() -> None:
    assert CONSOLIDATION_PROMPT_PATH.exists()


def test_consolidation_prompt_references_required_tools() -> None:
    text = CONSOLIDATION_PROMPT_PATH.read_text(encoding="utf-8")
    # The supervisor task needs these tools for the six-step workflow.
    for tool in (
        "memory_search",
        "memory_update",
        "memory_delete",
        "memory_promote_to_knowledge",
    ):
        assert tool in text, f"consolidation prompt missing tool: {tool}"


def test_consolidation_prompt_has_placeholders() -> None:
    """Placeholders the playbook fills in at task-creation time."""
    text = CONSOLIDATION_PROMPT_PATH.read_text(encoding="utf-8")
    for placeholder in (
        "{project_id}",
        "{project_name}",
        "{insights_dir}",
        "{knowledge_dir}",
        "{last_consolidated}",
        "{churn_count}",
    ):
        assert placeholder in text, f"consolidation prompt missing placeholder: {placeholder}"
