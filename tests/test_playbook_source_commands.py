"""Tests for the playbook source/create/delete commands used by the dashboard authoring loop.

Covers:
  - get_playbook_source: read markdown + source_hash by id
  - update_playbook_source: atomic write + sync compile, with optimistic-concurrency
  - create_playbook: writes new file at scope-appropriate vault path, then compiles
  - delete_playbook: archives source to vault/trash + unregisters from manager
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.commands.handler import CommandHandler


PB_MD = """\
---
id: test-playbook
triggers:
  - git.commit
scope: system
---

# Test

Do something then finish.
"""


def _make_compiled_playbook(**overrides):
    from src.playbooks.models import CompiledPlaybook, PlaybookNode

    defaults = dict(
        id="test-playbook",
        version=1,
        source_hash="abcdef1234567890",
        triggers=["git.commit"],
        scope="system",
        nodes={
            "start": PlaybookNode(entry=True, prompt="Do something.", goto="end"),
            "end": PlaybookNode(terminal=True),
        },
    )
    defaults.update(overrides)
    return CompiledPlaybook(**defaults)


def _make_compilation_result(*, success=True, playbook=None, errors=None, **overrides):
    from src.playbooks.compiler import CompilationResult

    if success and playbook is None:
        playbook = _make_compiled_playbook()
    defaults = dict(
        success=success,
        playbook=playbook,
        errors=errors or [],
        source_hash="abcdef1234567890",
        retries_used=0,
        skipped=False,
    )
    defaults.update(overrides)
    return CompilationResult(**defaults)


def _make_handler(tmp_path: Path, *, compile_result=None, has_pm=True):
    vault = tmp_path / "vault"
    (vault / "system" / "playbooks").mkdir(parents=True)
    (vault / "projects" / "my-app" / "playbooks").mkdir(parents=True)

    mock_orch = MagicMock()
    mock_orch.db = AsyncMock()
    mock_config = MagicMock()
    mock_config.data_dir = str(tmp_path)

    if has_pm:
        pm = AsyncMock()
        pm._source_paths = {}
        if compile_result is None:
            compile_result = _make_compilation_result()
        pm.compile_playbook = AsyncMock(return_value=compile_result)
        pm.remove_playbook = AsyncMock(return_value=True)
        mock_orch.playbook_manager = pm
    else:
        mock_orch.playbook_manager = None

    return CommandHandler(mock_orch, mock_config), vault


# ---------------------------------------------------------------------------
# get_playbook_source
# ---------------------------------------------------------------------------


class TestGetPlaybookSource:
    async def test_missing_id(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        result = await handler._cmd_get_playbook_source({})
        assert "error" in result
        assert "playbook_id" in result["error"]

    async def test_unknown_id(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        result = await handler._cmd_get_playbook_source({"playbook_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_reads_source_with_hash(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        md_path = vault / "system" / "playbooks" / "test-playbook.md"
        md_path.write_text(PB_MD, encoding="utf-8")

        result = await handler._cmd_get_playbook_source({"playbook_id": "test-playbook"})

        assert result["playbook_id"] == "test-playbook"
        assert result["markdown"] == PB_MD
        assert result["source_hash"]
        assert result["path"] == str(md_path)

    async def test_uses_manager_cached_path(self, tmp_path):
        """When the manager already knows the source path, no vault scan is needed."""
        handler, _ = _make_handler(tmp_path)
        off_vault = tmp_path / "other" / "custom.md"
        off_vault.parent.mkdir(parents=True)
        off_vault.write_text(PB_MD, encoding="utf-8")
        handler.orchestrator.playbook_manager._source_paths = {"test-playbook": str(off_vault)}

        result = await handler._cmd_get_playbook_source({"playbook_id": "test-playbook"})

        assert result["path"] == str(off_vault)


# ---------------------------------------------------------------------------
# update_playbook_source
# ---------------------------------------------------------------------------


class TestUpdatePlaybookSource:
    async def test_missing_args(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        assert "error" in await handler._cmd_update_playbook_source({})
        assert "error" in await handler._cmd_update_playbook_source({"playbook_id": "x"})

    async def test_unknown_playbook(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        result = await handler._cmd_update_playbook_source(
            {"playbook_id": "nope", "markdown": PB_MD}
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_happy_path_writes_and_compiles(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        md_path = vault / "system" / "playbooks" / "test-playbook.md"
        md_path.write_text(PB_MD, encoding="utf-8")
        new_md = PB_MD.replace("Do something", "Do something else")

        result = await handler._cmd_update_playbook_source(
            {"playbook_id": "test-playbook", "markdown": new_md}
        )

        assert result["compiled"] is True
        assert result["playbook_id"] == "test-playbook"
        assert result["version"] == 1
        # Atomic write succeeded — file has new content, no .tmp left behind
        assert md_path.read_text(encoding="utf-8") == new_md
        leftover = list(md_path.parent.glob(".*.tmp"))
        assert leftover == []
        # Manager.compile_playbook was called with force=True
        call_kwargs = handler.orchestrator.playbook_manager.compile_playbook.call_args[1]
        assert call_kwargs["force"] is True

    async def test_conflict_when_expected_hash_mismatches(self, tmp_path):
        from src.playbooks.compiler import PlaybookCompiler

        handler, vault = _make_handler(tmp_path)
        md_path = vault / "system" / "playbooks" / "test-playbook.md"
        md_path.write_text(PB_MD, encoding="utf-8")
        new_md = PB_MD.replace("Do something", "Do something else")

        result = await handler._cmd_update_playbook_source(
            {
                "playbook_id": "test-playbook",
                "markdown": new_md,
                "expected_source_hash": "stale-hash-value",
            }
        )

        assert result["error"] == "conflict"
        assert result["reason"] == "vault_changed_underneath"
        assert result["current_source_hash"] == PlaybookCompiler._compute_source_hash(PB_MD)
        # File must not have been overwritten
        assert md_path.read_text(encoding="utf-8") == PB_MD
        # Compile must not have been called
        handler.orchestrator.playbook_manager.compile_playbook.assert_not_awaited()

    async def test_matching_expected_hash_allows_write(self, tmp_path):
        from src.playbooks.compiler import PlaybookCompiler

        handler, vault = _make_handler(tmp_path)
        md_path = vault / "system" / "playbooks" / "test-playbook.md"
        md_path.write_text(PB_MD, encoding="utf-8")
        new_md = PB_MD.replace("Do something", "Do something else")

        result = await handler._cmd_update_playbook_source(
            {
                "playbook_id": "test-playbook",
                "markdown": new_md,
                "expected_source_hash": PlaybookCompiler._compute_source_hash(PB_MD),
            }
        )

        assert result["compiled"] is True
        assert md_path.read_text(encoding="utf-8") == new_md

    async def test_compile_failure_surfaces_errors(self, tmp_path):
        failing = _make_compilation_result(
            success=False,
            playbook=None,
            errors=["schema violation: missing entry node"],
            retries_used=2,
        )
        handler, vault = _make_handler(tmp_path, compile_result=failing)
        md_path = vault / "system" / "playbooks" / "test-playbook.md"
        md_path.write_text(PB_MD, encoding="utf-8")

        result = await handler._cmd_update_playbook_source(
            {"playbook_id": "test-playbook", "markdown": PB_MD + "\nextra\n"}
        )

        assert result["compiled"] is False
        assert result["errors"] == ["schema violation: missing entry node"]
        assert result["retries_used"] == 2


# ---------------------------------------------------------------------------
# create_playbook
# ---------------------------------------------------------------------------


class TestCreatePlaybook:
    async def test_missing_args(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        assert "error" in await handler._cmd_create_playbook({})
        assert "error" in await handler._cmd_create_playbook({"playbook_id": "a"})
        assert "error" in await handler._cmd_create_playbook(
            {"playbook_id": "a", "scope": "system"}
        )

    async def test_invalid_scope(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        result = await handler._cmd_create_playbook(
            {"playbook_id": "new-pb", "scope": "weird", "markdown": PB_MD}
        )
        assert "error" in result
        assert "Invalid scope" in result["error"]

    async def test_system_scope_writes_to_system_playbooks(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        result = await handler._cmd_create_playbook(
            {"playbook_id": "new-pb", "scope": "system", "markdown": PB_MD}
        )
        assert result["created"] is True
        target = vault / "system" / "playbooks" / "new-pb.md"
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == PB_MD

    async def test_project_scope_creates_intermediate_dirs(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        result = await handler._cmd_create_playbook(
            {
                "playbook_id": "proj-pb",
                "scope": "project:fresh-project",
                "markdown": PB_MD,
            }
        )
        assert result["created"] is True
        target = vault / "projects" / "fresh-project" / "playbooks" / "proj-pb.md"
        assert target.is_file()

    async def test_agent_type_scope(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        result = await handler._cmd_create_playbook(
            {
                "playbook_id": "coding-pb",
                "scope": "agent-type:coding",
                "markdown": PB_MD,
            }
        )
        assert result["created"] is True
        target = vault / "agent-types" / "coding" / "playbooks" / "coding-pb.md"
        assert target.is_file()

    async def test_rejects_collision_with_existing(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        existing = vault / "system" / "playbooks" / "already-here.md"
        existing.write_text(PB_MD, encoding="utf-8")

        result = await handler._cmd_create_playbook(
            {
                "playbook_id": "already-here",
                "scope": "system",
                "markdown": PB_MD,
            }
        )
        assert "error" in result
        assert "already exists" in result["error"]
        handler.orchestrator.playbook_manager.compile_playbook.assert_not_awaited()


# ---------------------------------------------------------------------------
# delete_playbook
# ---------------------------------------------------------------------------


class TestDeletePlaybook:
    async def test_missing_id(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        assert "error" in await handler._cmd_delete_playbook({})

    async def test_unknown_id(self, tmp_path):
        handler, _ = _make_handler(tmp_path)
        result = await handler._cmd_delete_playbook({"playbook_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_archives_source_and_unregisters(self, tmp_path):
        handler, vault = _make_handler(tmp_path)
        md_path = vault / "system" / "playbooks" / "doomed.md"
        md_path.write_text(PB_MD, encoding="utf-8")

        result = await handler._cmd_delete_playbook({"playbook_id": "doomed"})

        assert result["deleted"] is True
        assert result["removed_from_registry"] is True
        # Source file no longer at original location
        assert not md_path.exists()
        # Archive file exists in vault/trash/playbooks/ with the id prefix
        archived = Path(result["archived_path"])
        assert archived.is_file()
        assert archived.parent == vault / "trash" / "playbooks"
        assert archived.name.startswith("doomed.")
        assert archived.name.endswith(".md")
        # Archive has original content
        assert archived.read_text(encoding="utf-8") == PB_MD
        # remove_playbook was called on the manager
        handler.orchestrator.playbook_manager.remove_playbook.assert_awaited_once_with("doomed")
