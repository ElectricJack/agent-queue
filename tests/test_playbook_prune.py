"""Tests for ``PlaybookManager.prune_orphan_compilations``.

The prune step runs at startup after ``load_from_disk``. It walks the
flat ``{data_dir}/playbooks/compiled/*.json`` layout and removes any
compiled entry whose source ``.md`` no longer exists under the vault
playbook patterns. It also removes the playbook from the active
registry and clears its triggers / cooldown state.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.playbooks.manager import PlaybookManager


_VALID_COMPILED_NODES = {
    "nodes": {
        "start": {"entry": True, "prompt": "Do the work.", "goto": "end"},
        "end": {"terminal": True},
    }
}


def _make_mock_provider(num_compilations: int) -> AsyncMock:
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


def _write_md(vault_root: Path, rel_dir: str, filename: str, playbook_id: str) -> Path:
    target_dir = vault_root / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\n"
        f"id: {playbook_id}\n"
        f"triggers:\n"
        f"  - manual\n"
        f"scope: system\n"
        f"---\n\n"
        f"# {playbook_id}\n"
    )
    path = target_dir / filename
    path.write_text(body, encoding="utf-8")
    return path


def _drop_compiled_json(data_dir: Path, playbook_id: str) -> Path:
    """Write a minimal valid compiled.json directly to disk (no compilation)."""
    compiled_dir = data_dir / "playbooks" / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)
    path = compiled_dir / f"{playbook_id}.json"
    doc = {
        "id": playbook_id,
        "version": 1,
        "source_hash": "deadbeef",
        "triggers": ["manual"],
        "scope": "system",
        "nodes": _VALID_COMPILED_NODES["nodes"],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_orphan_compiled_json_removed(tmp_path: Path) -> None:
    """A compiled .json with no matching .md is deleted from disk."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    # Seed an orphan compiled JSON — no .md in vault.
    json_path = _drop_compiled_json(tmp_path, "orphan-playbook")
    assert json_path.exists()

    manager = PlaybookManager(chat_provider=None, data_dir=str(tmp_path))

    result = await manager.prune_orphan_compilations(str(vault_root))

    assert result["pruned"] == ["orphan-playbook"]
    assert result["checked"] == 1
    assert not json_path.exists()


@pytest.mark.asyncio
async def test_compiled_with_matching_md_kept(tmp_path: Path) -> None:
    """A compiled .json whose source .md exists is preserved."""
    vault_root = tmp_path / "vault"
    _write_md(vault_root, "system/playbooks", "keeper.md", "keeper")
    json_path = _drop_compiled_json(tmp_path, "keeper")

    manager = PlaybookManager(chat_provider=None, data_dir=str(tmp_path))

    result = await manager.prune_orphan_compilations(str(vault_root))
    assert result["pruned"] == []
    assert result["checked"] == 1
    assert json_path.exists()


@pytest.mark.asyncio
async def test_mixed_orphans_and_valid(tmp_path: Path) -> None:
    """Mixed set — orphans pruned, valid entries untouched."""
    vault_root = tmp_path / "vault"
    _write_md(vault_root, "system/playbooks", "keeper.md", "keeper")
    _write_md(vault_root, "agent-types/claude-code/playbooks", "r.md", "reflection")

    keeper_json = _drop_compiled_json(tmp_path, "keeper")
    refl_json = _drop_compiled_json(tmp_path, "reflection")
    orphan_json = _drop_compiled_json(tmp_path, "orphan")

    manager = PlaybookManager(chat_provider=None, data_dir=str(tmp_path))
    result = await manager.prune_orphan_compilations(str(vault_root))

    assert sorted(result["pruned"]) == ["orphan"]
    assert result["checked"] == 3
    assert keeper_json.exists()
    assert refl_json.exists()
    assert not orphan_json.exists()


@pytest.mark.asyncio
async def test_frontmatter_id_differs_from_filename(tmp_path: Path) -> None:
    """A .md whose frontmatter id differs from its filename still protects the JSON."""
    vault_root = tmp_path / "vault"
    _write_md(vault_root, "system/playbooks", "filename-stem.md", "real-id-in-frontmatter")

    # Compiled JSON uses the frontmatter id, not the filename.
    json_path = _drop_compiled_json(tmp_path, "real-id-in-frontmatter")

    manager = PlaybookManager(chat_provider=None, data_dir=str(tmp_path))
    result = await manager.prune_orphan_compilations(str(vault_root))
    assert result["pruned"] == []
    assert json_path.exists()


@pytest.mark.asyncio
async def test_orphan_removed_from_active_registry(tmp_path: Path) -> None:
    """If an orphaned playbook is already in `_active`, prune drops it too."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    # Compile a real playbook first (populates _active), then delete its .md
    # file so it becomes an orphan from the registry's perspective.
    md_path = _write_md(vault_root, "system/playbooks", "transient.md", "transient")
    provider = _make_mock_provider(num_compilations=1)
    manager = PlaybookManager(chat_provider=provider, data_dir=str(tmp_path))
    await manager.compile_playbook(md_path.read_text(), source_path=str(md_path))
    assert manager.get_playbook("transient") is not None

    # Yank the .md out from under it.
    md_path.unlink()

    result = await manager.prune_orphan_compilations(str(vault_root))
    assert result["pruned"] == ["transient"]
    assert manager.get_playbook("transient") is None


@pytest.mark.asyncio
async def test_no_compiled_dir_is_noop(tmp_path: Path) -> None:
    """Empty / missing compiled dir returns cleanly."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    manager = PlaybookManager(chat_provider=None, data_dir=str(tmp_path))
    result = await manager.prune_orphan_compilations(str(vault_root))
    assert result == {"pruned": [], "checked": 0}


def test_playbook_id_by_source_path_matches(tmp_path: Path) -> None:
    """Source-path lookup returns the correct id for an active playbook."""
    manager = PlaybookManager(chat_provider=None, data_dir=str(tmp_path))
    manager._source_paths["alpha"] = "/vault/system/playbooks/alpha.md"
    manager._source_paths["beta"] = "/vault/system/playbooks/beta.md"

    assert (
        manager.playbook_id_by_source_path("/vault/system/playbooks/alpha.md") == "alpha"
    )
    assert (
        manager.playbook_id_by_source_path("/vault/system/playbooks/beta.md") == "beta"
    )
    assert manager.playbook_id_by_source_path("/nonexistent.md") is None
