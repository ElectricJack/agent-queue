"""Tests for the 'cancelled' playbook_runs status and cancel_playbook_run.

Covers the migration (Task 1: CHECK constraint accepts 'cancelled') and,
once Task 2 lands, the command itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.models import PlaybookRun, PlaybookRunStatus
from src.database import Database
from tests.test_playbook_commands import _make_handler


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "cancel.db"))
    await d.initialize()
    yield d
    await d.close()


async def test_cancelled_status_accepted_by_check_constraint(db):
    """The CHECK constraint on playbook_runs.status allows 'cancelled'."""
    run = PlaybookRun(
        run_id="cancel-1",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="cancelled",
        started_at=1.0,
        completed_at=2.0,
    )
    await db.create_playbook_run(run)

    fetched = await db.get_playbook_run("cancel-1")
    assert fetched is not None
    assert fetched.status == "cancelled"


async def test_update_playbook_run_to_cancelled(db):
    """update_playbook_run can transition an existing row to 'cancelled'."""
    run = PlaybookRun(
        run_id="cancel-2",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="running",
        started_at=1.0,
    )
    await db.create_playbook_run(run)

    await db.update_playbook_run("cancel-2", status="cancelled", completed_at=5.0)

    fetched = await db.get_playbook_run("cancel-2")
    assert fetched.status == "cancelled"
    assert fetched.completed_at == 5.0


def test_playbook_run_status_enum_has_cancelled():
    assert PlaybookRunStatus.CANCELLED.value == "cancelled"


def test_cancelled_is_terminal():
    from src.playbooks.state_machine import TERMINAL_STATUSES

    assert PlaybookRunStatus.CANCELLED in TERMINAL_STATUSES


async def test_cancel_playbook_run_error_format():
    """cancel_playbook_run with missing args returns dict with 'error' key."""
    handler = _make_handler()

    result = await handler._cmd_cancel_playbook_run({})

    assert isinstance(result, dict)
    assert "error" in result


async def test_cancel_playbook_run_not_found():
    handler = _make_handler()
    handler.db.get_playbook_run = AsyncMock(return_value=None)

    result = await handler._cmd_cancel_playbook_run({"run_id": "missing"})

    assert "error" in result
    assert "missing" in result["error"]


async def test_cancel_playbook_run_rejects_terminal_run():
    handler = _make_handler()
    completed_run = PlaybookRun(
        run_id="r1",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="completed",
        started_at=1.0,
        completed_at=2.0,
    )
    handler.db.get_playbook_run = AsyncMock(return_value=completed_run)

    result = await handler._cmd_cancel_playbook_run({"run_id": "r1"})

    assert "error" in result
    assert "completed" in result["error"]
    handler.db.update_playbook_run.assert_not_called()


async def test_cancel_playbook_run_marks_running_run_cancelled():
    handler = _make_handler()
    running_run = PlaybookRun(
        run_id="r2",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="running",
        started_at=1.0,
        current_node="review",
        tokens_used=42,
    )
    handler.db.get_playbook_run = AsyncMock(return_value=running_run)
    handler.db.update_playbook_run = AsyncMock()

    result = await handler._cmd_cancel_playbook_run({"run_id": "r2"})

    assert result == {"cancelled": "r2", "playbook_id": "pb", "status": "cancelled"}
    handler.db.update_playbook_run.assert_awaited_once()
    _, kwargs = handler.db.update_playbook_run.await_args
    assert kwargs["status"] == "cancelled"
    assert "completed_at" in kwargs


async def test_cancel_playbook_run_marks_paused_run_cancelled():
    handler = _make_handler()
    paused_run = PlaybookRun(
        run_id="r3",
        playbook_id="pb",
        playbook_version=1,
        trigger_event="{}",
        status="paused",
        started_at=1.0,
    )
    handler.db.get_playbook_run = AsyncMock(return_value=paused_run)
    handler.db.update_playbook_run = AsyncMock()

    result = await handler._cmd_cancel_playbook_run({"run_id": "r3"})

    assert result["status"] == "cancelled"


async def test_cancel_playbook_run_emits_notify_event():
    handler = _make_handler()
    running_run = PlaybookRun(
        run_id="r4",
        playbook_id="pb",
        playbook_version=1,
        trigger_event='{"project_id": "demo"}',
        status="running",
        started_at=1.0,
        current_node="review",
    )
    handler.db.get_playbook_run = AsyncMock(return_value=running_run)
    handler.db.update_playbook_run = AsyncMock()
    handler.orchestrator.bus = AsyncMock()

    await handler._cmd_cancel_playbook_run({"run_id": "r4"})

    handler.orchestrator.bus.emit.assert_awaited_once()
    event_type, payload = handler.orchestrator.bus.emit.await_args.args
    assert event_type == "notify.playbook_run_cancelled"
    assert payload["run_id"] == "r4"
    assert payload["playbook_id"] == "pb"
    assert payload["node_id"] == "review"
    assert payload["project_id"] == "demo"
