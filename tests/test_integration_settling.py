"""Approvals share a bounded project integration settling window."""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from src.database.tables import project_integration_schedules
from src.integration.settling import (
    SETTLING_CAP_SECONDS,
    SETTLING_EXTENSION_SECONDS,
    clear,
    note_approval,
    settled,
)
from src.models import Project


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("integration-settling.db")
    await database.create_project(Project(id="p", name="settling project"))
    async with database.immediate() as conn:
        await conn.execute(
            insert(project_integration_schedules).values(
                project_id="p", interval_seconds=300, next_due_at=1300.0, updated_at=1000.0
            )
        )
    yield database


async def _window(db):
    async with db._engine.connect() as conn:
        return (
            await conn.execute(
                select(
                    project_integration_schedules.c.settling_first_approval_at,
                    project_integration_schedules.c.settling_fires_at,
                ).where(project_integration_schedules.c.project_id == "p")
            )
        ).one()


async def test_first_approval_arms_the_window(db):
    async with db.immediate() as conn:
        result = await note_approval(conn, project_id="p", now=1000.0)
    assert result == {
        "outcome": "armed",
        "project_id": "p",
        "first_approval_at": 1000.0,
        "fires_at": 1000.0 + SETTLING_EXTENSION_SECONDS,
    }
    assert await _window(db) == (1000.0, 1300.0)


async def test_later_approval_extends_the_window(db):
    async with db.immediate() as conn:
        await note_approval(conn, project_id="p", now=1000.0)
        result = await note_approval(conn, project_id="p", now=1100.0)
    assert result == {
        "outcome": "extended",
        "project_id": "p",
        "first_approval_at": 1000.0,
        "fires_at": 1100.0 + SETTLING_EXTENSION_SECONDS,
    }
    assert await _window(db) == (1000.0, 1400.0)


async def test_extension_is_capped_from_first_approval(db):
    async with db.immediate() as conn:
        await note_approval(conn, project_id="p", now=1000.0)
        result = await note_approval(
            conn, project_id="p", now=1000.0 + SETTLING_CAP_SECONDS - 60.0
        )
        after_cap = await note_approval(
            conn, project_id="p", now=1000.0 + SETTLING_CAP_SECONDS + 10.0
        )
    expected = {
        "outcome": "capped",
        "project_id": "p",
        "first_approval_at": 1000.0,
        "fires_at": 1000.0 + SETTLING_CAP_SECONDS,
    }
    assert result == expected
    assert after_cap == expected
    assert await _window(db) == (1000.0, 1000.0 + SETTLING_CAP_SECONDS)


async def test_settled_only_once_window_fires(db):
    async with db.immediate() as conn:
        assert await settled(conn, project_id="p", now=9999.0) is False
        await note_approval(conn, project_id="p", now=1000.0)
        assert await settled(conn, project_id="p", now=1299.0) is False
        assert await settled(conn, project_id="p", now=1300.0) is True


async def test_clear_disarms_the_window(db):
    async with db.immediate() as conn:
        await note_approval(conn, project_id="p", now=1000.0)
        await clear(conn, project_id="p")
        assert await settled(conn, project_id="p", now=9999.0) is False
    assert await _window(db) == (None, None)

    async with db.immediate() as conn:
        new_window = await note_approval(conn, project_id="p", now=4000.0)
    assert new_window == {
        "outcome": "armed",
        "project_id": "p",
        "first_approval_at": 4000.0,
        "fires_at": 4000.0 + SETTLING_EXTENSION_SECONDS,
    }
