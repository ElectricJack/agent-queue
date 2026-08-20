"""Tests for the aq-surface Phase S0 CommandHandler additions.

Covers ``get_schema``, ``task_show``, ``task_set`` (``src/commands/surface_commands.py``)
and the ``task_labels`` CRUD they depend on (``src/database/queries/task_queries.py``).
See docs/specs/implementation/aq-surface.md §3, §9 (Phase S0), §10 (Test Plan).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.config import AppConfig
from src.event_bus import EventBus
from src.models import Project, Task

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    from src.database.adapters.sqlite import SQLiteDatabaseAdapter

    adapter = SQLiteDatabaseAdapter(":memory:")
    await adapter.initialize()
    yield adapter
    await adapter.close()


@pytest.fixture
async def handler(db):
    from src.commands.handler import CommandHandler

    config = MagicMock()
    orchestrator = MagicMock()
    orchestrator.db = db
    return CommandHandler(orchestrator=orchestrator, config=config)


@pytest.fixture
async def prime_handler(db, tmp_path):
    """A CommandHandler wired with a real AppConfig (prime needs real vault
    paths) and a real EventBus (task_handoff emits session.restart_requested).
    """
    from src.commands.handler import CommandHandler

    config = AppConfig(data_dir=str(tmp_path / "data"))
    orchestrator = MagicMock()
    orchestrator.db = db
    orchestrator.bus = EventBus()
    return CommandHandler(orchestrator=orchestrator, config=config)


@pytest.fixture
async def task(db):
    """A task belonging to a real project, ready for show/set exercises."""
    await db.create_project(Project(id="proj-1", name="Test Project"))
    t = Task(id="task-1", project_id="proj-1", title="Do the thing", description="desc")
    await db.create_task(t)
    return t


# ---------------------------------------------------------------------------
# get_schema
# ---------------------------------------------------------------------------


class TestGetSchema:
    async def test_returns_schema_version_and_enums(self, handler):
        result = await handler.execute("get_schema", {})
        assert "error" not in result
        assert result["schema_version"] == 1
        assert set(result["enums"].keys()) == {
            "task_status",
            "task_type",
            "dependency_type",
            "gate_type",
            "gate_status",
        }

    async def test_task_status_enum_matches_models(self, handler):
        from src.models import TaskStatus

        result = await handler.execute("get_schema", {})
        assert result["enums"]["task_status"] == [s.value for s in TaskStatus]

    async def test_dependency_type_enum_matches_tables(self, handler):
        from src.database.tables import TASK_DEP_TYPES

        result = await handler.execute("get_schema", {})
        assert result["enums"]["dependency_type"] == list(TASK_DEP_TYPES)

    async def test_gate_type_and_status_match_tables(self, handler):
        from src.database.tables import GATE_STATUSES, GATE_TYPES

        result = await handler.execute("get_schema", {})
        assert result["enums"]["gate_type"] == list(GATE_TYPES)
        assert result["enums"]["gate_status"] == list(GATE_STATUSES)


# ---------------------------------------------------------------------------
# task_show
# ---------------------------------------------------------------------------


class TestTaskShow:
    async def test_missing_task_id(self, handler):
        result = await handler.execute("task_show", {})
        assert "error" in result

    async def test_unknown_task(self, handler):
        result = await handler.execute("task_show", {"task_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_composes_fields_context_and_labels(self, handler, db, task):
        await db.add_task_context(task.id, type="note", label="note", content="hello from context")
        await db.add_task_label(task.id, "urgent")

        result = await handler.execute("task_show", {"task_id": task.id})
        assert "error" not in result
        assert result["id"] == "task-1"
        assert result["title"] == "Do the thing"
        assert result["labels"] == ["urgent"]
        assert len(result["context"]) == 1
        assert result["context"][0]["content"] == "hello from context"

    async def test_no_labels_or_context_is_empty_not_missing(self, handler, task):
        result = await handler.execute("task_show", {"task_id": task.id})
        assert result["labels"] == []
        assert result["context"] == []


# ---------------------------------------------------------------------------
# task_set
# ---------------------------------------------------------------------------


class TestTaskSet:
    async def test_missing_task_id(self, handler):
        result = await handler.execute("task_set", {})
        assert "error" in result

    async def test_unknown_task(self, handler):
        result = await handler.execute("task_set", {"task_id": "nope", "note": "x"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_no_fields_is_an_error(self, handler, task):
        result = await handler.execute("task_set", {"task_id": task.id})
        assert "error" in result

    async def test_branch_and_pr_url(self, handler, db, task):
        result = await handler.execute(
            "task_set",
            {"task_id": task.id, "branch": "feat/x", "pr_url": "https://example/pr/1"},
        )
        assert "error" not in result
        assert set(result["fields_changed"]) == {"branch_name", "pr_url"}

        updated = await db.get_task(task.id)
        assert updated.branch_name == "feat/x"
        assert updated.pr_url == "https://example/pr/1"

    async def test_never_touches_status(self, handler, db, task):
        before = await db.get_task(task.id)
        await handler.execute("task_set", {"task_id": task.id, "note": "progress update"})
        after = await db.get_task(task.id)
        assert after.status == before.status

    async def test_note_becomes_task_context(self, handler, db, task):
        await handler.execute("task_set", {"task_id": task.id, "note": "progress update"})
        contexts = await db.get_task_contexts(task.id)
        assert any(c["content"] == "progress update" for c in contexts)

    async def test_labels_add_and_remove(self, handler, db, task):
        result = await handler.execute("task_set", {"task_id": task.id, "labels_add": ["a", "b"]})
        assert set(await db.get_task_labels(task.id)) == {"a", "b"}
        assert "+label:a" in result["fields_changed"]
        assert "+label:b" in result["fields_changed"]

        result2 = await handler.execute("task_set", {"task_id": task.id, "labels_remove": ["a"]})
        assert await db.get_task_labels(task.id) == ["b"]
        assert "-label:a" in result2["fields_changed"]

    async def test_meta_round_trips_through_get_all_task_meta(self, handler, db, task):
        await handler.execute("task_set", {"task_id": task.id, "meta": {"foo": "bar", "count": 3}})
        meta = await db.get_all_task_meta(task.id)
        assert meta == {"foo": "bar", "count": 3}

    async def test_work_dir_stored_as_metadata(self, handler, db, task):
        await handler.execute("task_set", {"task_id": task.id, "work_dir": "/tmp/work"})
        assert await db.get_task_meta(task.id, "work_dir") == "/tmp/work"

    async def test_returns_task_show_shape_with_fields_changed(self, handler, task):
        result = await handler.execute("task_set", {"task_id": task.id, "note": "x"})
        # Same composition as task_show (fields + context + labels) plus the delta.
        assert result["id"] == task.id
        assert "context" in result
        assert "labels" in result
        assert "fields_changed" in result


# ---------------------------------------------------------------------------
# task_labels DB layer (src/database/queries/task_queries.py)
# ---------------------------------------------------------------------------


class TestTaskLabelsDbLayer:
    async def test_add_is_idempotent(self, db, task):
        await db.add_task_label(task.id, "dup")
        await db.add_task_label(task.id, "dup")
        assert await db.get_task_labels(task.id) == ["dup"]

    async def test_remove_missing_is_a_noop(self, db, task):
        await db.remove_task_label(task.id, "never-added")
        assert await db.get_task_labels(task.id) == []

    async def test_labels_sorted(self, db, task):
        await db.add_task_label(task.id, "zeta")
        await db.add_task_label(task.id, "alpha")
        assert await db.get_task_labels(task.id) == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# prime — Phase S1 (docs/specs/implementation/aq-surface.md §3)
# ---------------------------------------------------------------------------


class TestPrime:
    async def test_missing_task_id_is_an_error(self, prime_handler):
        result = await prime_handler.execute("prime", {})
        assert "error" in result
        assert "task_id" in result["error"]

    async def test_unknown_task_is_an_error(self, prime_handler):
        result = await prime_handler.execute("prime", {"task_id": "nope"})
        assert "error" in result

    async def test_success_shape(self, prime_handler, db, task):
        result = await prime_handler.execute("prime", {"task_id": task.id})
        assert result["success"] is True
        assert isinstance(result["body"], str)
        assert isinstance(result["sections"], list)
        assert {s["key"] for s in result["sections"]} >= {"task", "tool_guidance"}
        assert result["source"] == "default"
        assert isinstance(result["tokens_est"], int)
        assert task.id in result["body"]

    async def test_session_id_and_work_dir_are_forwarded(self, prime_handler, db, task):
        result = await prime_handler.execute(
            "prime", {"task_id": task.id, "session_id": "sess-1", "work_dir": "/work/x"}
        )
        assert result["success"] is True
        workspaces = next(s for s in result["sections"] if s["key"] == "workspaces")
        assert "/work/x" in workspaces["body"]

    async def test_not_gated_by_memory_pause(self, prime_handler, db, task):
        # `prime` itself is not a memory command — the paused gate must not
        # touch it even though its L1/L2 slots render empty.
        result = await prime_handler.execute("prime", {"task_id": task.id})
        assert "error" not in result or result.get("success") is True


# ---------------------------------------------------------------------------
# task_handoff — Phase S1 (design §6.1)
# ---------------------------------------------------------------------------


class TestTaskHandoff:
    async def test_missing_task_id_is_an_error(self, prime_handler):
        result = await prime_handler.execute("task_handoff", {})
        assert "error" in result

    async def test_unknown_task_is_an_error(self, prime_handler):
        result = await prime_handler.execute("task_handoff", {"task_id": "nope"})
        assert "error" in result

    async def test_writes_handoff_task_context_row(self, prime_handler, db, task):
        result = await prime_handler.execute(
            "task_handoff",
            {"task_id": task.id, "subject": "partial fix", "detail": "stopped early"},
        )
        assert result["success"] is True
        assert result["handoff_id"]

        contexts = await db.get_task_contexts(task.id)
        handoff_rows = [c for c in contexts if c["type"] == "handoff"]
        assert len(handoff_rows) == 1
        payload = json.loads(handoff_rows[0]["content"])
        assert payload["subject"] == "partial fix"
        assert payload["detail"] == "stopped early"

    async def test_non_auto_requests_restart_and_emits_event(self, prime_handler, db, task):
        received = []
        prime_handler.orchestrator.bus.subscribe(
            "session.restart_requested", lambda data: received.append(data)
        )

        result = await prime_handler.execute("task_handoff", {"task_id": task.id})
        assert result["restart_requested"] is True
        assert len(received) == 1
        assert received[0]["task_id"] == task.id
        assert received[0]["reason"] == "handoff"

    async def test_auto_never_requests_restart_or_emits_event(self, prime_handler, db, task):
        received = []
        prime_handler.orchestrator.bus.subscribe(
            "session.restart_requested", lambda data: received.append(data)
        )

        result = await prime_handler.execute("task_handoff", {"task_id": task.id, "auto": True})
        assert result["restart_requested"] is False
        assert received == []

    async def test_handoff_note_appears_in_next_prime_render(self, prime_handler, db, task):
        await prime_handler.execute(
            "task_handoff", {"task_id": task.id, "auto": True, "subject": "s", "detail": "d"}
        )
        result = await prime_handler.execute("prime", {"task_id": task.id})
        messages = next(s for s in result["sections"] if s["key"] == "messages")
        assert "s" in messages["body"]
        assert "d" in messages["body"]
