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


async def test_spec_approve_rejects_non_markdown_path(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    vault_root = Path(handler.config.vault_root)
    spec_dir = vault_root / "projects" / "p1" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "not-a-spec.txt"
    spec_path.write_text("---\nstatus: draft\n---\n")
    r = await handler.execute(
        "spec_approve",
        {"project_id": "p1", "spec_path": str(spec_path)},
    )
    assert r["success"] is False
    assert ".md" in r["error"]


async def test_spec_approve_preserves_comments_and_order(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    vault_root = Path(handler.config.vault_root)
    spec_dir = vault_root / "projects" / "p1" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "2026-08-22-commented.md"
    original = textwrap.dedent("""\
        ---
        # Spec identity — chosen at draft time
        id: my-spec
        title: My Spec  # human-friendly
        status: draft
        # Owner should never change
        owner: jack
        ---
        # body
        """)
    spec_path.write_text(original)

    r = await handler.execute(
        "spec_approve",
        {"project_id": "p1", "spec_path": str(spec_path)},
    )
    assert r["success"] is True, r
    text = spec_path.read_text()
    assert "# Spec identity — chosen at draft time" in text
    assert "# human-friendly" in text
    assert "# Owner should never change" in text
    # Key order preserved (id before title before status before owner).
    fm_body = text.split("---", 2)[1]
    assert fm_body.index("id:") < fm_body.index("title:")
    assert fm_body.index("title:") < fm_body.index("status:")
    assert fm_body.index("status:") < fm_body.index("owner:")
    assert "status: approved" in text
