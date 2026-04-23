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


def test_delegates_via_create_task(playbook_text: str) -> None:
    """The playbook must delegate work to a created task, not do it itself."""
    assert "create_task" in playbook_text


def test_does_not_filter_task_to_supervisor_agent_type(playbook_text: str) -> None:
    """Consolidation tasks must NOT set agent_type=supervisor.

    task.agent_type is a hard scheduler filter against workspace-bound
    agent instances (e.g. "claude"). Setting it to "supervisor" leaves
    the task stuck in READY because no workspace agent advertises that
    type. The executing workspace agent can run the consolidation
    prompt unchanged using Read/Edit/Write/Bash on the vault files.
    """
    assert 'agent_type": "supervisor"' not in playbook_text
    assert "agent_type: \"supervisor\"" not in playbook_text
    assert "`agent_type`: `\"supervisor\"`" not in playbook_text


def test_respects_churn_threshold(playbook_text: str) -> None:
    """Timer runs must skip low-churn projects to avoid wasted runs.

    Manual invocations bypass thresholds (user intent wins), so we only
    enforce the rule on the timer path. Current rule: churn_count >= 5
    and last_consolidated older than 3 days.
    """
    assert "churn_count >= 5" in playbook_text
    assert "3 days" in playbook_text


def test_branches_on_manual_event(playbook_text: str) -> None:
    """Manual invocations must short-circuit — no churn check."""
    assert "event.project_id" in playbook_text
    assert "manual" in playbook_text.lower()


def test_discord_playbook_run_injects_channel_project(playbook_text: str) -> None:
    """The playbook relies on event.project_id being populated from the
    invoking Discord channel. Keep the contract with the Discord command
    documented in the playbook so the two stay in sync."""
    assert "project_id" in playbook_text


def test_references_consolidation_prompt(playbook_text: str) -> None:
    """Verifies the playbook instructs the supervisor to use the prompt template."""
    assert "consolidation_task.md" in playbook_text


def test_uses_scoped_vault_memory_tools(playbook_text: str) -> None:
    """Timer-run vault inspection must go through the dedicated tools.

    The ordinary ``read_file`` / ``list_directory`` tools are sandboxed to
    the workspace and cannot see ``~/.agent-queue/vault/``. The playbook
    must direct the supervisor to use ``read_project_memory_file`` and
    ``count_project_memory_files`` instead.
    """
    assert "read_project_memory_file" in playbook_text, (
        "Timer-run step should use read_project_memory_file to read "
        "consolidation.md, not read_file (which cannot access the vault)."
    )
    assert "count_project_memory_files" in playbook_text, (
        "Timer-run step should use count_project_memory_files to count "
        "insight churn, not list_directory (which cannot access the vault)."
    )


def test_does_not_reference_nonexistent_count_tool(playbook_text: str) -> None:
    """Guard against regressing to the phantom ``count_files_by_mtime``.

    That tool name appeared in an earlier revision but was never actually
    registered as a tool — the supervisor would silently fall through to a
    path-traversal failure. Keep the playbook pointed at real tools.
    """
    assert "count_files_by_mtime" not in playbook_text


def test_consolidation_prompt_exists() -> None:
    assert CONSOLIDATION_PROMPT_PATH.exists()


def test_consolidation_prompt_uses_direct_filesystem_tools() -> None:
    """The consolidation task edits vault markdown files directly.

    We deliberately avoid the memory_* MCP commands — they wrap Milvus,
    which drifts from whatever the agent does to the files, and the
    vault watcher re-indexes what's left behind. The prompt must lean
    on the default Claude toolset instead.
    """
    text = CONSOLIDATION_PROMPT_PATH.read_text(encoding="utf-8")
    for tool in ("Glob", "Read", "Edit", "Write", "Bash"):
        assert tool in text, f"consolidation prompt missing tool reference: {tool}"
    # Guard against regression to the old MCP-tool wording.
    for banned in (
        "memory_search",
        "memory_update",
        "memory_delete",
        "memory_promote_to_knowledge",
        "memory_store",
    ):
        assert banned not in text, (
            f"consolidation prompt should not reference the {banned} MCP "
            "command — edit the vault files directly instead."
        )


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
