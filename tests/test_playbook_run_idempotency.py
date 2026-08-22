"""Tests for playbook-run idempotency via event_id unique constraint.

Verifies:
- PlaybookRun.event_id field is present and persisted correctly.
- The UNIQUE partial index on (playbook_id, event_id) rejects duplicate runs.
- NULL event_ids do not collide (partial index only covers non-NULL values).
- get_playbook_run_by_event returns existing run for a (playbook_id, event_id) pair.
- _on_playbook_trigger: TOCTOU — IntegrityError from create_playbook_run aborts dispatch.
- _on_playbook_trigger: pre-check dedup path skips dispatch when row already exists.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy.exc

from src.database import Database
from src.models import PlaybookRun


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "pi.db"))
    await d.initialize()
    yield d
    await d.close()


async def test_duplicate_event_id_rejected(db):
    r1 = PlaybookRun(
        run_id="r1",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="running",
        started_at=1.0,
        event_id="evt-1",
    )
    await db.create_playbook_run(r1)

    r2 = PlaybookRun(
        run_id="r2",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="running",
        started_at=2.0,
        event_id="evt-1",
    )
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await db.create_playbook_run(r2)


async def test_null_event_ids_do_not_collide(db):
    for run_id in ("r1", "r2"):
        await db.create_playbook_run(
            PlaybookRun(
                run_id=run_id,
                playbook_id="pb",
                playbook_version=1,
                trigger_event="{}",
                status="running",
                started_at=1.0,
                event_id=None,
            )
        )
    # If we get here without IntegrityError, two NULL event_ids coexist — correct.
    runs = await db.list_playbook_runs(playbook_id="pb")
    assert len(runs) == 2


async def test_get_playbook_run_by_event_finds_existing(db):
    run = PlaybookRun(
        run_id="r-abc",
        playbook_id="my-pb",
        playbook_version=2,
        trigger_event="{}",
        status="running",
        started_at=42.0,
        event_id="stable-evt",
    )
    await db.create_playbook_run(run)

    found = await db.get_playbook_run_by_event("my-pb", "stable-evt")
    assert found is not None
    assert found.run_id == "r-abc"
    assert found.event_id == "stable-evt"


async def test_get_playbook_run_by_event_returns_none_when_missing(db):
    result = await db.get_playbook_run_by_event("no-pb", "no-event")
    assert result is None


async def test_event_id_persisted_and_hydrated(db):
    run = PlaybookRun(
        run_id="r-hydrate",
        playbook_id="pb2",
        playbook_version=1,
        trigger_event="{}",
        status="running",
        started_at=10.0,
        event_id="hydrate-evt",
    )
    await db.create_playbook_run(run)

    fetched = await db.get_playbook_run("r-hydrate")
    assert fetched is not None
    assert fetched.event_id == "hydrate-evt"


async def test_same_event_id_different_playbooks_allowed(db):
    """Same event_id can appear for different playbook_ids (partial unique on both cols)."""
    for pb_id, run_id in (("pb-a", "r-a"), ("pb-b", "r-b")):
        await db.create_playbook_run(
            PlaybookRun(
                run_id=run_id,
                playbook_id=pb_id,
                playbook_version=1,
                trigger_event="{}",
                status="running",
                started_at=1.0,
                event_id="shared-evt",
            )
        )
    # Both inserts succeed — different playbook_id, same event_id is allowed.
    assert await db.get_playbook_run_by_event("pb-a", "shared-evt") is not None
    assert await db.get_playbook_run_by_event("pb-b", "shared-evt") is not None


# ---------------------------------------------------------------------------
# Orchestrator _on_playbook_trigger dispatch-path tests
# ---------------------------------------------------------------------------
#
# These tests exercise the actual _on_playbook_trigger method on a minimal
# stub of Orchestrator that only wires up the attributes the method reads:
#   - self.db       (async db with get_playbook_run_by_event / create_playbook_run)
#   - self._command_handler
#   - self.llm_logger  (not used by pipeline path but imported early)
#
# We import the bound method directly so we test the real code, not a mock.


def _make_pipeline_playbook(pb_id: str = "pb-pipe") -> MagicMock:
    """Return a minimal playbook stub that looks like a pipeline kind."""
    pb = MagicMock()
    pb.id = pb_id
    pb.version = 1
    pb.to_dict.return_value = {
        "id": pb_id,
        "version": 1,
        "kind": "pipeline",
        "role": "default-pipeline",
        "nodes": {"done": {"terminal": True, "entry": True}},
    }
    return pb


def _make_orchestrator_stub(db_mock: MagicMock) -> MagicMock:
    """Return a minimal object that satisfies _on_playbook_trigger's attribute access."""
    from src.orchestrator.core import Orchestrator

    orch = MagicMock(spec=Orchestrator)
    orch.db = db_mock
    orch._command_handler = MagicMock()
    orch.llm_logger = None
    # Bind the real method so it executes against our stub.
    orch._on_playbook_trigger = Orchestrator._on_playbook_trigger.__get__(orch, Orchestrator)
    return orch


async def test_integrity_error_prevents_pipeline_dispatch():
    """TOCTOU fix: IntegrityError from create_playbook_run must abort dispatch."""
    db_mock = MagicMock()
    db_mock.get_playbook_run_by_event = AsyncMock(return_value=None)  # pre-check passes
    db_mock.create_playbook_run = AsyncMock(
        side_effect=sqlalchemy.exc.IntegrityError("stmt", {}, Exception("dupe"))
    )

    orch = _make_orchestrator_stub(db_mock)
    playbook = _make_pipeline_playbook()
    event_data = {"type": "task.completed", "event_id": "evt-race", "project_id": "P"}

    dispatched: list[object] = []

    with patch("asyncio.create_task", side_effect=lambda coro, **kw: dispatched.append(coro)):
        await orch._on_playbook_trigger(playbook, event_data)

    assert dispatched == [], "No pipeline task should be dispatched after IntegrityError"
    db_mock.create_playbook_run.assert_awaited_once()


async def test_other_exception_prevents_pipeline_dispatch():
    """Any non-IntegrityError DB failure must also abort dispatch."""
    db_mock = MagicMock()
    db_mock.get_playbook_run_by_event = AsyncMock(return_value=None)
    db_mock.create_playbook_run = AsyncMock(side_effect=RuntimeError("db gone"))

    orch = _make_orchestrator_stub(db_mock)
    playbook = _make_pipeline_playbook()
    event_data = {"type": "task.completed", "event_id": "evt-err", "project_id": "P"}

    dispatched: list[object] = []
    with patch("asyncio.create_task", side_effect=lambda coro, **kw: dispatched.append(coro)):
        await orch._on_playbook_trigger(playbook, event_data)

    assert dispatched == [], "No pipeline task should be dispatched after DB error"


async def test_pre_check_dedup_skips_dispatch():
    """Pre-check path: existing run found → return without dispatching."""
    existing_run = MagicMock()
    existing_run.run_id = "r-existing"

    db_mock = MagicMock()
    db_mock.get_playbook_run_by_event = AsyncMock(return_value=existing_run)
    db_mock.create_playbook_run = AsyncMock()

    orch = _make_orchestrator_stub(db_mock)
    playbook = _make_pipeline_playbook()
    event_data = {"type": "task.completed", "event_id": "evt-dup", "project_id": "P"}

    dispatched: list[object] = []
    with patch("asyncio.create_task", side_effect=lambda coro, **kw: dispatched.append(coro)):
        await orch._on_playbook_trigger(playbook, event_data)

    assert dispatched == [], "No pipeline task should be dispatched when run already exists"
    db_mock.create_playbook_run.assert_not_awaited()


async def test_happy_path_dispatches_pipeline():
    """Sanity: no existing row + successful insert → pipeline task IS dispatched."""
    db_mock = MagicMock()
    db_mock.get_playbook_run_by_event = AsyncMock(return_value=None)
    db_mock.create_playbook_run = AsyncMock(return_value=None)

    orch = _make_orchestrator_stub(db_mock)
    playbook = _make_pipeline_playbook()
    event_data = {"type": "task.completed", "event_id": "evt-new", "project_id": "P"}

    dispatched: list[object] = []
    with patch("asyncio.create_task", side_effect=lambda coro, **kw: dispatched.append(coro)):
        await orch._on_playbook_trigger(playbook, event_data)

    assert len(dispatched) == 1, "Pipeline task must be dispatched on the happy path"
    db_mock.create_playbook_run.assert_awaited_once()


async def test_pipeline_run_row_marked_completed():
    """The run row must leave 'running' once the pipeline finishes."""
    db_mock = MagicMock()
    db_mock.get_playbook_run_by_event = AsyncMock(return_value=None)
    db_mock.create_playbook_run = AsyncMock(return_value=None)
    db_mock.update_playbook_run = AsyncMock(return_value=None)

    orch = _make_orchestrator_stub(db_mock)
    playbook = _make_pipeline_playbook()
    event_data = {"type": "task.completed", "event_id": "evt-done", "project_id": "P"}

    dispatched: list[object] = []
    with patch("asyncio.create_task", side_effect=lambda coro, **kw: dispatched.append(coro)):
        await orch._on_playbook_trigger(playbook, event_data)

    assert len(dispatched) == 1
    await dispatched[0]  # run the pipeline to completion

    db_mock.update_playbook_run.assert_awaited_once()
    kwargs = db_mock.update_playbook_run.await_args.kwargs
    assert kwargs["status"] == "completed"
    assert kwargs["error"] is None
    assert kwargs["completed_at"] > 0
