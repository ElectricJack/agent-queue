"""Tests for pane_open on _cmd_message_send."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import MessagesConfig
from src.database import Database
from src.models import Project
from src.panes.registry import SERVER_PANE_REGISTRY, PaneEntry


@pytest.fixture
def registry_with_test_view():
    SERVER_PANE_REGISTRY["__test-view"] = PaneEntry(id="__test-view", agent_pushable=True)
    SERVER_PANE_REGISTRY["__test-locked"] = PaneEntry(id="__test-locked", agent_pushable=False)
    yield
    SERVER_PANE_REGISTRY.pop("__test-view", None)
    SERVER_PANE_REGISTRY.pop("__test-locked", None)


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "panes.db"))
    await db.initialize()
    await db.create_project(Project(id="demo", name="demo"))
    orch = MagicMock()
    orch.db = db
    orch.bus = MagicMock()
    orch.bus.emit = AsyncMock()
    config = MagicMock()
    config.messages = MessagesConfig(enabled=True)
    h = CommandHandler(orch, config)
    h._active_project_id = None
    yield h
    await db.close()


def _base_args(**over):
    args = {
        "project_id": "demo",
        "to_kind": "user",
        "to_id": "dashboard",
        "from_kind": "user",
        "from_id": "test",
        "body": "opened the pane",
    }
    args.update(over)
    return args


@pytest.mark.asyncio
async def test_message_send_accepts_pane_open(handler, registry_with_test_view):
    r = await handler._cmd_message_send(
        _base_args(pane_open={"view": "__test-view", "args": {"taskId": "t1"}})
    )
    assert r.get("message_id"), r
    assert r["state"] == "queued"
    assert r["message"]["pane_open"] == {"view": "__test-view", "args": {"taskId": "t1"}}
    assert r["message"]["body_kind"] == "pane_open"


@pytest.mark.asyncio
async def test_message_send_rejects_unknown_view(handler, registry_with_test_view):
    r = await handler._cmd_message_send(
        _base_args(body="x", pane_open={"view": "does-not-exist", "args": {}})
    )
    assert "unknown pane view" in r.get("error", "").lower()


@pytest.mark.asyncio
async def test_message_send_rejects_non_pushable_view(handler, registry_with_test_view):
    r = await handler._cmd_message_send(
        _base_args(body="x", pane_open={"view": "__test-locked", "args": {}})
    )
    assert "not agent-pushable" in r.get("error", "").lower()
