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
    payload = approved[-1][1]
    assert payload["project_id"] == "p1"
    assert payload["spec_path"] == str(spec_path.resolve())
    assert payload["decision_id"] == r["decision_id"]
    assert payload["packet_sha256"] == r["packet_sha256"]
    assert payload["review_id"] is None and payload["revision"] is None
    assert payload["spec_kind"] == "design"



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


async def test_standalone_snapshot_pins_bytes_provenance_and_same_body_reuses_decision(handler):
    import hashlib
    from sqlalchemy import select
    from src.database.tables import ask_decisions, approved_document_snapshots

    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    path = Path(handler.config.vault_root) / "projects/p1/specs/exact.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\r\n# Café\r\n  Preserve whitespace.  \r\n".encode()
    path.write_bytes(b"---\r\nstatus: draft\r\nspec_kind: implementation\r\n---" + body)
    args = {"project_id": "p1", "spec_path": str(path)}
    first = await handler.execute("spec_approve", args)
    assert first["success"], first
    replay = await handler.execute("spec_approve", args)
    assert replay["success"] and replay["decision_id"] == first["decision_id"], replay
    async with handler.db._engine.connect() as conn:
        snapshot = await handler.db.get_approved_document_snapshot(first["snapshot_id"], conn=conn)
        assert snapshot["body"] == body
        assert snapshot["body_sha256"] == hashlib.sha256(body).hexdigest()
        assert snapshot["approval_provenance"]["principal_id"] == "human:local-operator"
        assert len((await conn.execute(select(ask_decisions))).all()) == 1
    path.write_bytes(b"---\nstatus: approved\nspec_kind: implementation\n---\nChanged body\n")
    changed = await handler.execute("spec_approve", args)
    assert changed["success"] and changed["decision_id"] != first["decision_id"]
    async with handler.db._engine.connect() as conn:
        assert len((await conn.execute(select(approved_document_snapshots))).all()) == 2
        assert (await handler.db.get_approved_document_snapshot(first["snapshot_id"], conn=conn))["body"] == body


async def test_review_binding_survives_frontmatter_removal(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    submitted = await handler.execute("review_submit", {
        "project_id": "p1", "kind": "spec", "title": "Reviewed source", "content": "# Reviewed",
    })
    assert submitted["success"], submitted
    review = await handler.db.get_review(submitted["review_id"])
    path = Path(handler.config.vault_root) / review["vault_path"]
    path.write_text("---\nstatus: approved\n---\nCaller edited mirror")
    result = await handler.execute("spec_approve", {"project_id": "p1", "spec_path": str(path)})
    assert not result["success"] and result["error_code"] == "review_gate", result
    assert submitted["review_id"] in result["error"]
