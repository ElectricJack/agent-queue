"""Tests for playbook-run idempotency via event_id unique constraint.

Verifies:
- PlaybookRun.event_id field is present and persisted correctly.
- The UNIQUE partial index on (playbook_id, event_id) rejects duplicate runs.
- NULL event_ids do not collide (partial index only covers non-NULL values).
- get_playbook_run_by_event returns existing run for a (playbook_id, event_id) pair.
"""
from __future__ import annotations

import pytest

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
