from __future__ import annotations

from pathlib import Path

import pytest

from src.assignment_routing import AssignmentPlaybookError, select_assignment_playbook
from src.models import Project
from src.playbooks.compiler import compile_playbook
from src.playbooks.manager import PlaybookManager
from src.vault import ensure_default_playbooks


ASSIGNMENT_MARKDOWN = """---
id: project-router
kind: assignment-routing
role: assignment-routing
scope: project
triggers:
  - assignment.route.requested
max_tokens: 1024
---
Choose the least expensive class that can reliably complete each task.
"""


def test_assignment_markdown_compiles_to_one_llm_node() -> None:
    result = compile_playbook(ASSIGNMENT_MARKDOWN)

    assert result.success, result.errors
    assert result.playbook is not None
    assert result.playbook.kind == "assignment-routing"
    assert result.playbook.role == "assignment-routing"
    assert list(result.playbook.nodes) == ["choose", "done"]
    assert result.playbook.nodes["choose"].entry
    assert result.playbook.nodes["choose"].goto == "done"
    assert "Never choose profile_id" in result.playbook.nodes["choose"].prompt
    assert result.playbook.max_tokens == 1024
    assert result.playbook.validate() == []


def test_assignment_compiler_default_budget_fits_a_full_batch() -> None:
    result = compile_playbook(ASSIGNMENT_MARKDOWN.replace("max_tokens: 1024\n", ""))

    assert result.success
    assert result.playbook.max_tokens == 4096


@pytest.mark.asyncio
async def test_manager_installs_assignment_playbook_without_compile_task() -> None:
    manager = PlaybookManager(config=None)

    result = await manager.compile_playbook(
        ASSIGNMENT_MARKDOWN,
        source_path="project-router.md",
        scope_identifier="p",
    )

    assert result.success
    assert manager.get_playbook("project-router") is result.playbook
    assert manager.get_scope_identifier("project-router") == "p"


def test_assignment_compiler_rejects_wrong_role() -> None:
    result = compile_playbook(ASSIGNMENT_MARKDOWN.replace(
        "role: assignment-routing", "role: reviewer"
    ))
    assert not result.success
    assert any("assignment-routing" in error for error in result.errors)


def test_disabled_assignment_playbook_round_trips() -> None:
    playbook = compile_playbook(ASSIGNMENT_MARKDOWN.replace(
        "max_tokens: 1024", "max_tokens: 1024\nenabled: false"
    )).playbook
    assert playbook is not None and not playbook.enabled
    assert type(playbook).from_dict(playbook.to_dict()).enabled is False


class _Manager:
    def __init__(self, playbook, scope_identifier=None):
        self.playbook = playbook
        self.scope_identifier = scope_identifier

    def get_playbook(self, playbook_id):
        return self.playbook if self.playbook and self.playbook.id == playbook_id else None

    def get_scope_identifier(self, playbook_id):
        return self.scope_identifier


def test_null_project_override_uses_system_default() -> None:
    default = compile_playbook(
        ASSIGNMENT_MARKDOWN.replace("project-router", "default-assignment-routing").replace(
            "scope: project", "scope: system"
        )
    ).playbook
    selected = select_assignment_playbook(_Manager(default), Project(id="p", name="P"))
    assert selected is default


def test_project_override_must_match_project_scope() -> None:
    custom = compile_playbook(ASSIGNMENT_MARKDOWN).playbook
    project = Project(id="p", name="P", assignment_playbook_id="project-router")

    with pytest.raises(AssignmentPlaybookError, match="scoped to project"):
        select_assignment_playbook(_Manager(custom, "other"), project)

    assert select_assignment_playbook(_Manager(custom, "p"), project) is custom


def test_explicit_broken_override_never_falls_back() -> None:
    project = Project(id="p", name="P", assignment_playbook_id="missing")
    with pytest.raises(AssignmentPlaybookError, match="missing"):
        select_assignment_playbook(_Manager(None), project)


def test_default_assignment_playbook_is_seeded_write_if_absent(tmp_path: Path) -> None:
    first = ensure_default_playbooks(str(tmp_path))
    second = ensure_default_playbooks(str(tmp_path))
    path = tmp_path / "vault" / "system" / "playbooks" / "default-assignment-routing.md"

    assert "default-assignment-routing.md" in first["created"]
    assert "default-assignment-routing.md" in second["skipped"]
    assert path.exists()


def test_default_assignment_playbook_uses_fast_low_router() -> None:
    source = (
        Path(__file__).parent.parent
        / "src"
        / "prompts"
        / "default_playbooks"
        / "default-assignment-routing.md"
    )

    result = compile_playbook(source.read_text(encoding="utf-8"))

    assert result.success
    assert result.playbook.llm_config.intelligence_class == "fast-low"
    assert result.playbook.max_tokens == 4096


def test_paused_playbook_subsystem_reports_config_error_not_attribute_error() -> None:
    """``playbooks.enabled=false`` leaves ``playbook_manager`` None (core.py
    feature-pause branch).  Selection must surface that as the ordinary
    "unavailable playbook" configuration error every call site already
    handles, not an AttributeError that escapes the guards."""
    with pytest.raises(AssignmentPlaybookError, match="playbook subsystem is disabled"):
        select_assignment_playbook(None, Project(id="p", name="P"))

    project = Project(id="p", name="P", assignment_playbook_id="project-router")
    with pytest.raises(AssignmentPlaybookError, match="playbook subsystem is disabled"):
        select_assignment_playbook(None, project)
