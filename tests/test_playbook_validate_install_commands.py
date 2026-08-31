"""Tests for playbook_validate + playbook_install commands (Phase 6)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _vault_dir(handler) -> Path:
    """Return the handler's vault root, creating it if missing."""
    root = Path(handler.config.vault_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


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
async def test_validate_valid_compiled(command_handler_factory):
    handler = await command_handler_factory()
    p = _vault_dir(handler) / "demo.json"
    p.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute("playbook_validate", {"path": str(p)})
    assert r["success"] is True
    assert r["errors"] == []


@pytest.mark.asyncio
async def test_validate_missing_entry_node(command_handler_factory):
    bad = dict(VALID_COMPILED)
    bad["nodes"] = {"end": {"terminal": True}}
    handler = await command_handler_factory()
    p = _vault_dir(handler) / "bad.json"
    p.write_text(json.dumps(bad))
    r = await handler.execute("playbook_validate", {"path": str(p)})
    assert r["success"] is False
    assert any("entry" in (e["message"] or "").lower() for e in r["errors"])


@pytest.mark.asyncio
async def test_validate_source_markdown_frontmatter_only(command_handler_factory):
    handler = await command_handler_factory()
    md = _vault_dir(handler) / "demo.md"
    md.write_text(
        "---\nid: demo\ntriggers: [task.completed]\nscope: system\n---\n# hi\n"
    )
    r = await handler.execute("playbook_validate", {"path": str(md)})
    assert r["success"] is True
    assert r.get("requires_compile") is True


@pytest.mark.asyncio
async def test_validate_missing_frontmatter_field(command_handler_factory):
    handler = await command_handler_factory()
    md = _vault_dir(handler) / "bad.md"
    md.write_text("---\nid: demo\n---\n# hi\n")
    r = await handler.execute("playbook_validate", {"path": str(md)})
    assert r["success"] is False
    fields = {e.get("field") for e in r["errors"]}
    assert "triggers" in fields or "scope" in fields


@pytest.mark.asyncio
async def test_install_round_trips(command_handler_factory):
    handler = await command_handler_factory()
    pm = _attach_stub_manager(handler)
    p = _vault_dir(handler) / "demo.json"
    p.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute(
        "playbook_install", {"playbook_id": "demo", "compiled_path": str(p)}
    )
    assert r["success"] is True
    got = pm.get_playbook("demo")
    assert got is not None
    assert got.id == "demo"


@pytest.mark.asyncio
async def test_install_rejects_invalid(command_handler_factory):
    handler = await command_handler_factory()
    _attach_stub_manager(handler)
    bad = dict(VALID_COMPILED)
    bad["nodes"] = {}
    p = _vault_dir(handler) / "bad.json"
    p.write_text(json.dumps(bad))
    r = await handler.execute(
        "playbook_install", {"playbook_id": "bad", "compiled_path": str(p)}
    )
    assert r["success"] is False
    assert r["errors"]


@pytest.mark.asyncio
async def test_install_rejects_id_mismatch(command_handler_factory):
    handler = await command_handler_factory()
    _attach_stub_manager(handler)
    p = _vault_dir(handler) / "demo.json"
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


# ---------------------------------------------------------------------------
# Vault-boundary guard: any file outside vault_root is rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_rejects_path_outside_vault(
    tmp_path, command_handler_factory
):
    handler = await command_handler_factory()
    outside = tmp_path / "escape.json"
    outside.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute("playbook_validate", {"path": str(outside)})
    assert r["success"] is False
    assert any("outside vault" in (e["message"] or "") for e in r["errors"])


@pytest.mark.asyncio
async def test_validate_rejects_dotdot_escape(
    tmp_path, command_handler_factory
):
    handler = await command_handler_factory()
    vault = _vault_dir(handler)
    # ../../../etc/passwd-like traversal from inside the vault path.
    escape = vault / ".." / ".." / "escape.json"
    escape.parent.mkdir(parents=True, exist_ok=True)
    escape.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute("playbook_validate", {"path": str(escape)})
    assert r["success"] is False
    assert any("outside vault" in (e["message"] or "") for e in r["errors"])


@pytest.mark.asyncio
async def test_install_rejects_symlink_escape(
    tmp_path, command_handler_factory
):
    """A symlink inside the vault pointing OUT of the vault must be
    rejected — ``resolve()`` dereferences the link so relative_to catches it."""
    handler = await command_handler_factory()
    _attach_stub_manager(handler)
    vault = _vault_dir(handler)
    real = tmp_path / "real.json"
    real.write_text(json.dumps(VALID_COMPILED))
    link = vault / "sneak.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    r = await handler.execute(
        "playbook_install",
        {"playbook_id": "demo", "compiled_path": str(link)},
    )
    assert r["success"] is False
    assert any(
        "outside vault" in (e["message"] or "") for e in r["errors"]
    )


@pytest.mark.asyncio
async def test_install_rejects_absolute_path_outside_vault(
    tmp_path, command_handler_factory
):
    handler = await command_handler_factory()
    _attach_stub_manager(handler)
    outside = tmp_path / "abs.json"
    outside.write_text(json.dumps(VALID_COMPILED))
    r = await handler.execute(
        "playbook_install",
        {"playbook_id": "demo", "compiled_path": str(outside)},
    )
    assert r["success"] is False
    assert any(
        "outside vault" in (e["message"] or "") for e in r["errors"]
    )


# ---------------------------------------------------------------------------
# _structure_errors — the node/field/message parse the compiler agent
# iterates against (coverage plan item 26)
# ---------------------------------------------------------------------------


def test_structured_errors_parse_node_field_message():
    from src.playbooks.validator_command import _structure_errors

    out = _structure_errors(
        [
            "Node 'review': command: must be present",
            "Node 'x': bare message",
            "Node 'walk' transition[0]: goto target 'gone' does not exist",
            "no node prefix at all",
        ]
    )
    assert out == [
        {"node": "review", "field": "command", "message": "must be present"},
        {"node": "x", "field": None, "message": "bare message"},
        {
            "node": "walk",
            "field": "transition[0]",
            "message": "goto target 'gone' does not exist",
        },
        {"node": None, "field": None, "message": "no node prefix at all"},
    ]


@pytest.mark.asyncio
async def test_install_reports_structured_error_when_manager_install_fails(
    command_handler_factory,
):
    """PB-5: a failed install (e.g. store save failure) surfaces as the
    command's structured-error convention, not a success."""
    handler = await command_handler_factory()

    class _FailingManager:
        async def install_compiled(self, compiled) -> None:
            raise RuntimeError("store save failed for playbook 'demo': disk full")

    handler.orchestrator.playbook_manager = _FailingManager()
    p = _vault_dir(handler) / "demo.json"
    p.write_text(json.dumps(VALID_COMPILED))

    r = await handler.execute(
        "playbook_install", {"playbook_id": "demo", "compiled_path": str(p)}
    )

    assert r["success"] is False
    assert any("store save failed" in e["message"] for e in r["errors"])
