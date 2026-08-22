"""spec_approve — frontmatter flip + spec.approved event (Phase 6 Task 5)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    yield h
    if hasattr(h, "_db") and h._db is not None:
        await h._db.close()


def _emitted(h) -> list[tuple[str, dict]]:
    calls = h.orchestrator.bus.emit.call_args_list
    out: list[tuple[str, dict]] = []
    for c in calls:
        args, kwargs = c
        if args:
            evt = args[0]
            payload = args[1] if len(args) > 1 else kwargs.get("payload", {})
        else:
            evt = kwargs.get("event_type") or kwargs.get("name")
            payload = kwargs.get("payload", {})
        out.append((evt, payload))
    return out


async def test_spec_approve_flips_frontmatter_and_emits_event(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})

    vault_root = Path(handler.config.vault_root)
    spec_dir = vault_root / "projects" / "p1" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "2026-08-21-foo.md"
    spec_path.write_text(
        textwrap.dedent("""\
            ---
            status: draft
            ---
            # foo
            """)
    )

    r = await handler.execute(
        "spec_approve",
        {"project_id": "p1", "spec_path": str(spec_path)},
    )
    assert r["success"] is True, r
    assert "status: approved" in spec_path.read_text()

    approved = [e for e in _emitted(handler) if e[0] == "spec.approved"]
    assert approved
    # The command resolves the spec path; compare against the resolved form.
    assert approved[-1][1] == {
        "project_id": "p1",
        "spec_path": str(spec_path.resolve()),
    }


async def test_spec_approve_rejects_path_escape(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    r = await handler.execute(
        "spec_approve",
        {"project_id": "p1", "spec_path": "/etc/passwd"},
    )
    assert r["success"] is False
    assert "outside" in r["error"].lower() or "path" in r["error"].lower()


async def test_spec_approve_requires_frontmatter(handler, tmp_path):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    vault_root = Path(handler.config.vault_root)
    spec_dir = vault_root / "projects" / "p1" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "no-frontmatter.md"
    spec_path.write_text("# no yaml here\n")
    r = await handler.execute(
        "spec_approve",
        {"project_id": "p1", "spec_path": str(spec_path)},
    )
    assert r["success"] is False
    assert "frontmatter" in r["error"].lower()
