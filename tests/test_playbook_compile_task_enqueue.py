"""Vault-watcher routes ordinary .md edits to a compile task (Phase 6 T9).

Pipeline playbooks keep the deterministic-parse path Phase 1 introduced.
Ordinary playbooks stop calling the LLM inline — the watcher enqueues a
task with ``profile_id=playbook-compiler`` and ``dedup_key=playbook-compile:<id>``.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.playbooks.handler import on_playbook_changed
from src.vault_watcher import VaultChange


PIPELINE_MD = textwrap.dedent("""\
    ---
    id: my-pipeline
    kind: pipeline
    role: custom
    scope: system
    triggers: [task.created]
    ---
    ```json
    {
      "entry": "n0",
      "nodes": {
        "n0": {"terminal": true}
      }
    }
    ```
""")

ORDINARY_MD = textwrap.dedent("""\
    ---
    id: my-quality-gate
    scope: system
    triggers: [task.completed]
    ---
    # Quality Gate
    Prompt-driven playbook…
""")


class _StubManager:
    """Minimal PlaybookManager stub for the watcher handler unit test."""

    def __init__(self, command_handler, project_id: str) -> None:
        self.command_handler = command_handler
        self._project_id = project_id
        self.pipeline_compiles: list[tuple[str, str]] = []

    async def compile_playbook_pipeline(
        self,
        markdown: str,
        *,
        source_path: str = "",
        rel_path: str = "",
        scope_identifier: str | None = None,
    ):
        self.pipeline_compiles.append((rel_path, markdown))
        result = MagicMock()
        result.success = True
        result.errors = []
        return result

    async def compile_task_project_id(self, scope, identifier):
        return self._project_id

    def playbook_id_by_source_path(self, source_path: str) -> str | None:
        return None

    async def remove_playbook(self, playbook_id: str) -> bool:
        return False


async def _make_project(handler, project_id: str = "sys") -> str:
    r = await handler.execute(
        "create_project", {"id": project_id, "name": project_id}
    )
    assert "error" not in r, r
    # Compile task pins the compiler profile — pre-create it in the DB so
    # _cmd_create_task's profile-lookup succeeds.
    from src.models import AgentProfile

    await handler.db.create_profile(
        AgentProfile(
            id="playbook-compiler",
            name="Playbook Compiler",
            model="claude-haiku-4-5-20251001",
            permission_mode="bypassPermissions",
            allowed_tools=[
                "playbook_validate",
                "playbook_install",
                "list_playbooks",
                "get_playbook",
            ],
        )
    )
    return project_id


async def _find_compile_task(handler, project_id: str, dedup_key: str) -> dict | None:
    """Look up the (non-terminal) compile task by dedup_key via the DB helper."""
    task = await handler.db.find_task_by_dedup_key(project_id, dedup_key)
    if task is None:
        return None
    r = await handler.execute("get_task", {"task_id": task.id})
    return r


@pytest.mark.asyncio
async def test_pipeline_md_still_compiles_inline(
    tmp_path, command_handler_factory
):
    handler = await command_handler_factory()
    project_id = await _make_project(handler)
    pm = _StubManager(handler, project_id)

    src = tmp_path / "my-pipeline.md"
    src.write_text(PIPELINE_MD)
    change = VaultChange(
        path=str(src),
        rel_path="system/playbooks/my-pipeline.md",
        operation="created",
    )
    await on_playbook_changed([change], playbook_manager=pm)

    assert pm.pipeline_compiles, "pipeline playbook should have taken deterministic path"
    # No compile task should be created for pipeline playbooks.
    t = await _find_compile_task(handler, project_id, "playbook-compile:my-pipeline")
    assert t is None


@pytest.mark.asyncio
async def test_ordinary_md_enqueues_compile_task(
    tmp_path, command_handler_factory
):
    handler = await command_handler_factory()
    project_id = await _make_project(handler)
    pm = _StubManager(handler, project_id)

    src = tmp_path / "my-quality-gate.md"
    src.write_text(ORDINARY_MD)
    change = VaultChange(
        path=str(src),
        rel_path="system/playbooks/my-quality-gate.md",
        operation="created",
    )
    await on_playbook_changed([change], playbook_manager=pm)

    t = await _find_compile_task(handler, project_id, "playbook-compile:my-quality-gate")
    assert t is not None
    assert t.get("profile_id") == "playbook-compiler"
    # Description carries the source path so the compiler agent can read it.
    assert str(src) in (t.get("description") or "")
    # And no inline LLM compile occurred (stub would have logged it).
    assert pm.pipeline_compiles == []


@pytest.mark.asyncio
async def test_ordinary_md_double_write_is_deduped(
    tmp_path, command_handler_factory
):
    handler = await command_handler_factory()
    project_id = await _make_project(handler)
    pm = _StubManager(handler, project_id)

    src = tmp_path / "dup.md"
    src.write_text(ORDINARY_MD.replace("my-quality-gate", "dup"))
    change = VaultChange(
        path=str(src),
        rel_path="system/playbooks/dup.md",
        operation="created",
    )
    await on_playbook_changed([change], playbook_manager=pm)

    first = await _find_compile_task(handler, project_id, "playbook-compile:dup")
    assert first is not None
    first_id = first["id"]

    # Second event for the same file (modified) — ensure_task dedup keeps it
    # to a single open task.
    src.write_text(src.read_text() + "\n<!-- edit -->\n")
    change2 = VaultChange(
        path=str(src),
        rel_path="system/playbooks/dup.md",
        operation="modified",
    )
    await on_playbook_changed([change2], playbook_manager=pm)

    again = await _find_compile_task(handler, project_id, "playbook-compile:dup")
    assert again is not None
    assert again["id"] == first_id  # same task, no new row


@pytest.mark.asyncio
async def test_failed_compile_task_is_cleared_on_re_edit(
    tmp_path, command_handler_factory
):
    """Editing a playbook after a FAILED compile clears the FAILED task
    and enqueues a fresh one.  Prevents both "one FAILED forever blocks
    retries" AND "each editor save duplicates the task"."""
    from src.models import TaskStatus

    handler = await command_handler_factory()
    project_id = await _make_project(handler)
    pm = _StubManager(handler, project_id)

    src = tmp_path / "brokenpb.md"
    src.write_text(ORDINARY_MD.replace("my-quality-gate", "brokenpb"))
    change = VaultChange(
        path=str(src),
        rel_path="system/playbooks/brokenpb.md",
        operation="created",
    )
    await on_playbook_changed([change], playbook_manager=pm)

    first = await handler.db.find_task_by_dedup_key(
        project_id, "playbook-compile:brokenpb"
    )
    assert first is not None
    # Simulate compiler agent failing.
    await handler.db.update_task(first.id, status=TaskStatus.FAILED)

    # Editor saves again — should clear the FAILED row and create fresh.
    src.write_text(src.read_text() + "\n<!-- fix attempt -->\n")
    change2 = VaultChange(
        path=str(src),
        rel_path="system/playbooks/brokenpb.md",
        operation="modified",
    )
    await on_playbook_changed([change2], playbook_manager=pm)

    # Old FAILED task should be gone (deleted).
    from src.database.tables import tasks as _tt
    from sqlalchemy import select as _select
    async with handler.db._engine.begin() as conn:
        rows = (
            await conn.execute(
                _select(_tt).where(_tt.c.id == first.id)
            )
        ).all()
    assert rows == [], "FAILED compile task should have been deleted"

    # Exactly one live compile task should exist.
    fresh = await handler.db.find_task_by_dedup_key(
        project_id, "playbook-compile:brokenpb"
    )
    assert fresh is not None
    assert fresh.id != first.id
    assert fresh.status != TaskStatus.FAILED


@pytest.mark.asyncio
async def test_missing_project_skips_enqueue(
    tmp_path, command_handler_factory, caplog
):
    handler = await command_handler_factory()
    pm = _StubManager(handler, project_id=None)  # type: ignore[arg-type]

    src = tmp_path / "orphan.md"
    src.write_text(ORDINARY_MD.replace("my-quality-gate", "orphan"))
    change = VaultChange(
        path=str(src),
        rel_path="system/playbooks/orphan.md",
        operation="created",
    )
    await on_playbook_changed([change], playbook_manager=pm)

    # No project → nothing created, no crash.
    projects = await handler.db.list_projects()
    for p in projects:
        t = await handler.db.find_task_by_dedup_key(p.id, "playbook-compile:orphan")
        assert t is None


def test_playbook_compiler_profile_ships_in_repo():
    """The playbook-compiler agent-type profile ships with the repo.

    Lives in ``src/profiles/defaults/`` (the daemon seeds it into
    ``vault/agent-types/`` at first run).
    """
    candidates = [
        Path("vault/agent-types/playbook-compiler/profile.md"),
        Path("src/profiles/defaults/playbook-compiler/profile.md"),
    ]
    hit = next((p for p in candidates if p.is_file()), None)
    assert hit is not None, f"missing profile in any of: {candidates}"
    text = hit.read_text(encoding="utf-8")
    assert "playbook-compiler" in text
    assert "playbook_validate" in text
    assert "playbook_install" in text
