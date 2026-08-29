# tests/test_hierarchy_queries.py
"""HierarchyQueryMixin — spec Part I (§4–§8)."""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import AgentState, Project, Task, TaskStatus

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


async def mktask(db, tid, status=TaskStatus.DEFINED, **kw):
    await db.create_task(
        Task(id=tid, project_id=PROJECT_ID, title=tid, description=tid, status=status, **kw)
    )
    return tid


class TestSchemaFields:
    async def test_new_task_columns_have_defaults(self, db):
        await mktask(db, "a")
        t = await db.get_task("a")
        assert t.created_by_kind is None
        assert t.created_by_id is None
        assert t.claim_epoch == 0
        assert t.filed_count == 0

    async def test_created_by_round_trips(self, db):
        await mktask(db, "a", created_by_kind="session", created_by_id="s-1")
        t = await db.get_task("a")
        assert (t.created_by_kind, t.created_by_id) == ("session", "s-1")

    def test_agent_state_has_retired(self):
        assert AgentState.RETIRED.value == "RETIRED"
