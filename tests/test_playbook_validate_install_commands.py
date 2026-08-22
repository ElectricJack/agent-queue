"""Tests for playbook_validate + playbook_install commands (Phase 6)."""
from __future__ import annotations

import json

import pytest


VALID_COMPILED = {
    "id": "demo",
    "version": 1,
    "source_hash": "deadbeef00000000",
    "scope": "system",
    "triggers": [{"event_type": "task.completed"}],
    "nodes": {
        "start": {"entry": True, "prompt": "hi", "goto": "end"},
        "end": {"terminal": True},
    },
}


class _StubPlaybookManager:
    """Minimal stand-in for PlaybookManager — records install_compiled calls."""

    def __init__(self) -> None:
        self._active: dict = {}

    async def install_compiled(self, compiled) -> None:
        self._active[compiled.id] = compiled

    def get_playbook(self, pid: str):
        return self._active.get(pid)


def _attach_stub_manager(handler) -> _StubPlaybookManager:
    pm = _StubPlaybookManager()
    handler.orchestrator.playbook_manager = pm
    return pm


@pytest.mark.asyncio
async def test_validate_valid_compiled(tmp_path, command_handler_factory):
    handler = await command_handler_factory()
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute("playbook_validate", {"path": str(p)})
    assert r["success"] is True
    assert r["errors"] == []


@pytest.mark.asyncio
async def test_validate_missing_entry_node(tmp_path, command_handler_factory):
    bad = dict(VALID_COMPILED)
    bad["nodes"] = {"end": {"terminal": True}}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    handler = await command_handler_factory()
    r = await handler.execute("playbook_validate", {"path": str(p)})
    assert r["success"] is False
    assert any("entry" in (e["message"] or "").lower() for e in r["errors"])


@pytest.mark.asyncio
async def test_validate_source_markdown_frontmatter_only(
    tmp_path, command_handler_factory
):
    md = tmp_path / "demo.md"
    md.write_text(
        "---\nid: demo\ntriggers: [task.completed]\nscope: system\n---\n# hi\n"
    )
    handler = await command_handler_factory()
    r = await handler.execute("playbook_validate", {"path": str(md)})
    assert r["success"] is True
    assert r.get("requires_compile") is True


@pytest.mark.asyncio
async def test_validate_missing_frontmatter_field(
    tmp_path, command_handler_factory
):
    md = tmp_path / "bad.md"
    md.write_text("---\nid: demo\n---\n# hi\n")
    handler = await command_handler_factory()
    r = await handler.execute("playbook_validate", {"path": str(md)})
    assert r["success"] is False
    fields = {e.get("field") for e in r["errors"]}
    assert "triggers" in fields or "scope" in fields


@pytest.mark.asyncio
async def test_install_round_trips(tmp_path, command_handler_factory):
    handler = await command_handler_factory()
    pm = _attach_stub_manager(handler)
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute(
        "playbook_install", {"playbook_id": "demo", "compiled_path": str(p)}
    )
    assert r["success"] is True
    got = pm.get_playbook("demo")
    assert got is not None
    assert got.id == "demo"


@pytest.mark.asyncio
async def test_install_rejects_invalid(tmp_path, command_handler_factory):
    handler = await command_handler_factory()
    _attach_stub_manager(handler)
    bad = dict(VALID_COMPILED)
    bad["nodes"] = {}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    r = await handler.execute(
        "playbook_install", {"playbook_id": "bad", "compiled_path": str(p)}
    )
    assert r["success"] is False
    assert r["errors"]


@pytest.mark.asyncio
async def test_install_rejects_id_mismatch(tmp_path, command_handler_factory):
    handler = await command_handler_factory()
    _attach_stub_manager(handler)
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute(
        "playbook_install",
        {"playbook_id": "other", "compiled_path": str(p)},
    )
    assert r["success"] is False


@pytest.mark.asyncio
async def test_validate_missing_path(command_handler_factory):
    handler = await command_handler_factory()
    r = await handler.execute("playbook_validate", {})
    assert r["success"] is False
    assert r["errors"]
