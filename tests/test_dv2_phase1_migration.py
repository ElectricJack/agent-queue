"""Verifies dv2 phase-1 column additions + gate-type extension are live."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from src.database import Database
from src.database.tables import GATE_TYPES

pytestmark = pytest.mark.migration


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "phase1.db"))
    await d.initialize()
    yield d
    await d.close()


async def test_tasks_has_dedup_key_and_intelligence_class(db):
    async with db._engine.begin() as conn:
        cols = await conn.run_sync(
            lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("tasks")}
        )
    assert "dedup_key" in cols
    assert "intelligence_class" in cols


async def test_agent_profiles_has_default_class_and_needs_workspace(db):
    async with db._engine.begin() as conn:
        cols = await conn.run_sync(
            lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("agent_profiles")}
        )
    assert "default_class" in cols
    assert "needs_workspace" in cols


async def test_playbook_runs_has_event_id_and_unique(db):
    async with db._engine.begin() as conn:
        cols = await conn.run_sync(
            lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("playbook_runs")}
        )
        idxs = await conn.run_sync(
            lambda sync_conn: [i["name"] for i in inspect(sync_conn).get_indexes("playbook_runs")]
        )
    assert "event_id" in cols
    assert any("pb_event" in n for n in idxs)


async def test_routing_in_gate_types():
    assert "routing" in GATE_TYPES
