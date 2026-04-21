"""Tests for ``PlaybookManager.reconcile_compilations``.

The reconcile method walks the vault for playbook markdown files and
compiles any whose id isn't already in the active registry. This fixes
the gap where freshly-installed default playbooks never trigger initial
compilation (the vault watcher snapshots them as pre-existing).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.playbooks.manager import PlaybookManager


# Minimal compiled JSON body the mock provider will return for every compile.
_VALID_COMPILED_NODES = {
    "nodes": {
        "start": {"entry": True, "prompt": "Do the work.", "goto": "end"},
        "end": {"terminal": True},
    }
}


def _make_mock_provider(num_compilations: int) -> AsyncMock:
    """Create a mock ChatProvider that returns a valid compiled JSON N times."""
    from src.chat_providers.types import ChatResponse, TextBlock

    provider = AsyncMock()
    provider.model_name = "test-model"
    json_str = json.dumps(_VALID_COMPILED_NODES, indent=2)
    responses = [
        ChatResponse(content=[TextBlock(text=f"```json\n{json_str}\n```")])
        for _ in range(num_compilations)
    ]
    provider.create_message = AsyncMock(side_effect=responses)
    return provider


def _write_playbook_md(
    vault_root: Path,
    rel_dir: str,
    filename: str,
    playbook_id: str,
    trigger: str = "manual",
    scope: str = "system",
) -> Path:
    """Write a minimal playbook .md under vault_root/rel_dir/filename."""
    target_dir = vault_root / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\n"
        f"id: {playbook_id}\n"
        f"triggers:\n"
        f"  - {trigger}\n"
        f"scope: {scope}\n"
        f"---\n\n"
        f"# {playbook_id}\n\nDo the work.\n"
    )
    path = target_dir / filename
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_compiles_uncompiled_playbook(tmp_path: Path) -> None:
    """A .md file in the vault with no matching compiled entry gets compiled."""
    vault_root = tmp_path / "vault"
    _write_playbook_md(
        vault_root,
        "system/playbooks",
        "new-playbook.md",
        "new-playbook",
    )

    provider = _make_mock_provider(num_compilations=1)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))

    result = await manager.reconcile_compilations(str(vault_root))
    assert result["compiled"] == ["new-playbook"]
    assert result["skipped"] == []
    assert result["errors"] == []
    assert manager.get_playbook("new-playbook") is not None


@pytest.mark.asyncio
async def test_skips_already_active_playbook(tmp_path: Path) -> None:
    """Playbooks already in the active registry are skipped."""
    vault_root = tmp_path / "vault"
    _write_playbook_md(
        vault_root,
        "system/playbooks",
        "existing.md",
        "existing",
    )

    # Pre-compile so the playbook is already in _active.
    provider = _make_mock_provider(num_compilations=1)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))
    md = (vault_root / "system" / "playbooks" / "existing.md").read_text()
    await manager.compile_playbook(md)
    assert manager.get_playbook("existing") is not None

    # Reconcile should see it's already compiled and skip it without
    # invoking the mock provider again.
    result = await manager.reconcile_compilations(str(vault_root))
    assert result["compiled"] == []
    assert result["skipped"] == ["existing"]
    # The provider was only invoked for the pre-compile, not the reconcile.
    assert provider.create_message.await_count == 1


@pytest.mark.asyncio
async def test_multiple_scopes(tmp_path: Path) -> None:
    """Reconcile handles system, agent-types, and project scopes."""
    vault_root = tmp_path / "vault"
    _write_playbook_md(
        vault_root, "system/playbooks", "sysplay.md", "sys-play"
    )
    _write_playbook_md(
        vault_root,
        "agent-types/supervisor/playbooks",
        "supplay.md",
        "sup-play",
        scope="agent-type:supervisor",
    )
    _write_playbook_md(
        vault_root,
        "projects/my-app/playbooks",
        "projplay.md",
        "proj-play",
        scope="project",
    )

    provider = _make_mock_provider(num_compilations=3)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))

    result = await manager.reconcile_compilations(str(vault_root))
    assert set(result["compiled"]) == {"sys-play", "sup-play", "proj-play"}
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_missing_id_recorded_as_error(tmp_path: Path) -> None:
    """Markdown without a frontmatter `id` is reported as an error, not skipped."""
    vault_root = tmp_path / "vault"
    bad_dir = vault_root / "system" / "playbooks"
    bad_dir.mkdir(parents=True)
    (bad_dir / "bad.md").write_text(
        "---\ntriggers:\n  - manual\nscope: system\n---\n\nNo id here.\n",
        encoding="utf-8",
    )

    provider = _make_mock_provider(num_compilations=0)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))

    result = await manager.reconcile_compilations(str(vault_root))
    assert result["compiled"] == []
    assert result["skipped"] == []
    assert len(result["errors"]) == 1
    assert "id" in result["errors"][0][1][0]


@pytest.mark.asyncio
async def test_nonexistent_vault_root_is_noop(tmp_path: Path) -> None:
    """An invalid vault path returns an empty result, not an exception."""
    provider = _make_mock_provider(num_compilations=0)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))

    result = await manager.reconcile_compilations(str(tmp_path / "does-not-exist"))
    assert result == {"compiled": [], "skipped": [], "errors": []}


@pytest.mark.asyncio
async def test_ignores_non_playbook_md(tmp_path: Path) -> None:
    """Markdown files outside the playbook path patterns are ignored."""
    vault_root = tmp_path / "vault"
    # A markdown file in a non-playbook location — should be skipped.
    (vault_root / "system" / "memory").mkdir(parents=True)
    (vault_root / "system" / "memory" / "notes.md").write_text(
        "---\nid: not-a-playbook\n---\n",
        encoding="utf-8",
    )

    provider = _make_mock_provider(num_compilations=0)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))

    result = await manager.reconcile_compilations(str(vault_root))
    assert result == {"compiled": [], "skipped": [], "errors": []}
