"""Behaviour tests for the aq-notes internal plugin.

Coverage plan §plugins items 9–13.  All commands are exercised through
``CommandHandler.execute()`` against a real SQLite database, a real
``EventBus``, and the real ``PluginRegistry`` with internal plugins
loaded, via the shared ``internal_plugins_handler`` conftest fixture (F1,
promoted per FU-13).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.models import Project

PROJECT_ID = "notes-proj"


async def _make_handler_with_project(internal_plugins_handler):
    handler = await internal_plugins_handler()
    await handler._db.create_project(Project(id=PROJECT_ID, name="Notes Project"))
    return handler


def _notes_dir(handler) -> Path:
    return Path(handler.config.data_dir) / "vault" / "projects" / PROJECT_ID / "notes"


def _subscribe(handler, event_type: str) -> list[dict]:
    captured: list[dict] = []

    async def _on_event(payload: dict) -> None:
        captured.append(payload)

    handler._bus.subscribe(event_type, _on_event)
    return captured


class TestWriteNote:
    async def test_write_note_creates_file_and_emits_note_created(self, internal_plugins_handler):
        handler = await _make_handler_with_project(internal_plugins_handler)
        created = _subscribe(handler, "note.created")
        updated = _subscribe(handler, "note.updated")

        result = await handler.execute(
            "write_note",
            {"project_id": PROJECT_ID, "title": "My First Note", "content": "body"},
        )

        assert result["status"] == "created"
        note_path = _notes_dir(handler) / "my-first-note.md"
        assert note_path.is_file()
        assert note_path.read_text(encoding="utf-8") == "body"

        assert len(created) == 1
        payload = created[0]
        assert payload["note_name"] == "my-first-note.md"
        assert payload["note_path"] == str(note_path)
        assert payload["title"] == "My First Note"
        assert payload["project_id"] == PROJECT_ID
        assert updated == []

        # Re-running the same write reports "updated" and emits note.updated.
        result = await handler.execute(
            "write_note",
            {"project_id": PROJECT_ID, "title": "My First Note", "content": "body v2"},
        )
        assert result["status"] == "updated"
        assert note_path.read_text(encoding="utf-8") == "body v2"
        assert len(created) == 1
        assert len(updated) == 1
        assert updated[0]["note_name"] == "my-first-note.md"

    @pytest.mark.parametrize("bad_title", ["///", "..."])
    async def test_write_note_rejects_title_that_slugifies_to_empty(
        self, internal_plugins_handler, bad_title
    ):
        handler = await _make_handler_with_project(internal_plugins_handler)
        result = await handler.execute(
            "write_note",
            {"project_id": PROJECT_ID, "title": bad_title, "content": "x"},
        )
        assert result == {"error": "Title produces an empty filename"}

        # No file may appear anywhere under the project's vault directory —
        # a path-ish title must not escape the notes dir.
        project_vault = Path(handler.config.data_dir) / "vault" / "projects" / PROJECT_ID
        files = [p for p in project_vault.rglob("*") if p.is_file()]
        assert files == []


class TestAppendNote:
    async def test_append_note_creates_with_h1_then_appends(self, internal_plugins_handler):
        handler = await _make_handler_with_project(internal_plugins_handler)
        created = _subscribe(handler, "note.created")
        updated = _subscribe(handler, "note.updated")

        first = await handler.execute(
            "append_note",
            {"project_id": PROJECT_ID, "title": "Meeting Log", "content": "content"},
        )
        assert first["status"] == "created"
        note_path = _notes_dir(handler) / "meeting-log.md"
        assert note_path.read_text(encoding="utf-8") == "# Meeting Log\n\ncontent"
        assert len(created) == 1

        second = await handler.execute(
            "append_note",
            {"project_id": PROJECT_ID, "title": "Meeting Log", "content": "content2"},
        )
        assert second["status"] == "appended"
        assert note_path.read_text(encoding="utf-8") == "# Meeting Log\n\ncontent\n\ncontent2"
        assert second["size_bytes"] > first["size_bytes"]
        assert len(updated) == 1
        assert updated[0]["operation"] == "appended"


class TestDeleteNote:
    async def test_delete_note_resolves_by_title_not_filename(self, internal_plugins_handler):
        handler = await _make_handler_with_project(internal_plugins_handler)
        deleted = _subscribe(handler, "note.deleted")

        await handler.execute(
            "write_note",
            {"project_id": PROJECT_ID, "title": "Release Plan", "content": "the plan"},
        )
        decoy = _notes_dir(handler) / "release-plan-old.md"
        decoy.write_text("# Old Plan\n\nstale", encoding="utf-8")

        result = await handler.execute(
            "delete_note", {"project_id": PROJECT_ID, "title": "Release Plan"}
        )

        target = _notes_dir(handler) / "release-plan.md"
        assert result["deleted"] == str(target)
        assert not target.exists()
        assert decoy.is_file(), "decoy file must survive a title-resolved delete"
        assert len(deleted) == 1
        assert deleted[0]["note_name"] == "release-plan.md"


class TestReadAndListNotes:
    async def test_read_and_list_notes_report_h1_title(self, internal_plugins_handler):
        handler = await _make_handler_with_project(internal_plugins_handler)
        notes_dir = _notes_dir(handler)
        os.makedirs(notes_dir, exist_ok=True)
        (notes_dir / "alpha-note.md").write_text(
            "# Completely Different Title\n\nalpha body", encoding="utf-8"
        )
        (notes_dir / "beta-note.md").write_text("no heading here", encoding="utf-8")
        (notes_dir / "not-a-note.txt").write_text("ignored", encoding="utf-8")

        listing = await handler.execute("list_notes", {"project_id": PROJECT_ID})
        by_name = {n["name"]: n for n in listing["notes"]}
        assert set(by_name) == {"alpha-note.md", "beta-note.md"}
        # H1 wins over the filename-derived title.
        assert by_name["alpha-note.md"]["title"] == "Completely Different Title"
        # No H1: title-cased slug with dashes as spaces.
        assert by_name["beta-note.md"]["title"] == "Beta Note"

        read = await handler.execute(
            "read_note", {"project_id": PROJECT_ID, "title": "alpha-note.md"}
        )
        assert read["content"] == "# Completely Different Title\n\nalpha body"
        assert read["size_bytes"] == len(read["content"])

        missing = await handler.execute(
            "read_note", {"project_id": PROJECT_ID, "title": "does-not-exist"}
        )
        assert missing == {"error": "Note 'does-not-exist' not found"}
