"""Tests for ``delete_project_profile`` covering both vault layouts.

A project-scoped profile may live at either:
  * nested:  ``vault/projects/<project>/agent-types/<type>/profile.md``
  * flat:    ``vault/agent-types/project:<project>:<type>/profile.md`` (legacy)

Older vaults still hold the flat-layout file and the startup scanner picks
either up. Delete must therefore unlink BOTH so the override doesn't keep
resurrecting itself.
"""

import os
from pathlib import Path

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.orchestrator import Orchestrator


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    from src.event_bus import EventBus
    from src.plugins.registry import PluginRegistry
    from src.plugins.services import build_internal_services

    orchestrator = Orchestrator(config)
    orchestrator.db = db

    services = build_internal_services(db=db, git=None, config=config)
    registry = PluginRegistry(db=db, bus=EventBus(), config=config)
    registry._internal_services = services
    await registry.load_internal_plugins()
    orchestrator.plugin_registry = registry

    h = CommandHandler(orchestrator, config)
    registry.set_active_project_id_getter(lambda: h._active_project_id)
    return h


def _profile_md(scoped_id: str) -> str:
    return f"---\nid: {scoped_id}\nname: '{scoped_id}'\n---\n\n# stub\n"


async def _write_profile(handler, path: str, scoped_id: str) -> None:
    """Write a profile file and sync it to the DB so delete sees a real row."""
    from src.profiles.sync import sync_profile_text_to_db

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(_profile_md(scoped_id), encoding="utf-8")
    await sync_profile_text_to_db(
        _profile_md(scoped_id), handler.db, source_path=path, fallback_id=scoped_id
    )


async def test_delete_removes_nested_layout(handler, config):
    nested = handler._cmd_create_project_profile  # exercise the real create flow
    result = await nested({"project_id": "proj", "agent_type": "supervisor"})
    assert "error" not in result, result
    nested_path = handler._vault_project_profile_path("proj", "supervisor")
    assert os.path.isfile(nested_path)

    out = await handler._cmd_delete_project_profile(
        {"project_id": "proj", "agent_type": "supervisor"}
    )
    assert out["deleted"] == "project:proj:supervisor"
    assert nested_path in out["removed_paths"]
    assert not os.path.isfile(nested_path)
    assert await handler.db.get_profile("project:proj:supervisor") is None


async def test_delete_removes_flat_layout_legacy(handler, config):
    """The actual bug: flat-layout file lingers and resurrects the override."""
    scoped_id = "project:proj:supervisor"
    flat_path = os.path.join(config.data_dir, "vault", "agent-types", scoped_id, "profile.md")
    await _write_profile(handler, flat_path, scoped_id)
    assert await handler.db.get_profile(scoped_id) is not None

    out = await handler._cmd_delete_project_profile(
        {"project_id": "proj", "agent_type": "supervisor"}
    )
    assert flat_path in out["removed_paths"]
    assert not os.path.isfile(flat_path)
    # parent dir cleaned up so the next startup scan finds nothing
    assert not os.path.isdir(os.path.dirname(flat_path))
    assert await handler.db.get_profile(scoped_id) is None


async def test_delete_removes_both_layouts_when_both_present(handler, config):
    scoped_id = "project:proj:supervisor"
    nested = handler._vault_project_profile_path("proj", "supervisor")
    flat = os.path.join(config.data_dir, "vault", "agent-types", scoped_id, "profile.md")
    await _write_profile(handler, nested, scoped_id)
    await _write_profile(handler, flat, scoped_id)

    out = await handler._cmd_delete_project_profile(
        {"project_id": "proj", "agent_type": "supervisor"}
    )
    assert set(out["removed_paths"]) == {nested, flat}
    assert not os.path.isfile(nested)
    assert not os.path.isfile(flat)


async def test_delete_missing_profile_errors(handler):
    out = await handler._cmd_delete_project_profile({"project_id": "proj", "agent_type": "nope"})
    assert "error" in out
    assert "not found" in out["error"]
