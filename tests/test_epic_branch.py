"""Epic branch names are legible slugs, reserved once per repository."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.database import Database
from src.database.tables import tasks
from src.integration.epic_branch import reserve_branch_name, slugify
from src.models import Project, RepoConfig, RepoSourceType, Task
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("epic-branch.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="epic branch project"))
    for repo_id in ("repo-a", "repo-b"):
        await database.create_repo(
            RepoConfig(
                id=repo_id,
                project_id="p",
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path),
            )
        )
    yield database
    await database.close()


async def _create(db, conn, task_id: str, title: str, *, repo_id: str | None = None):
    await db.create_task(
        Task(id=task_id, project_id="p", repo_id=repo_id, title=title, description=""),
        conn=conn,
    )


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Epic Pull Requests and the Train") == "epic-pull-requests-and-the-train"


def test_slugify_strips_punctuation_and_collapses_separators():
    assert slugify("Fix:  the   publisher's / delivery!!") == "fix-the-publisher-s-delivery"


def test_slugify_truncates_on_a_word_boundary():
    result = slugify("word " * 30)
    assert len(result) <= 60
    assert not result.endswith("-")


def test_slugify_falls_back_when_nothing_survives():
    assert slugify("!!! ???") == "epic"


async def test_reserve_uses_the_slug_for_an_epic(db):
    async with db.immediate() as conn:
        await _create(db, conn, "t1", "Retire the publisher")
        name = await reserve_branch_name(conn, task_id="t1", title="Retire the publisher", is_epic=True)
    assert name == "aq/epic/retire-the-publisher"


async def test_reserve_keeps_task_id_branches_for_children(db):
    async with db.immediate() as conn:
        await _create(db, conn, "t2", "Retire the publisher")
        name = await reserve_branch_name(conn, task_id="t2", title="Retire the publisher", is_epic=False)
    assert name == "aq/t2"


async def test_reserve_suffixes_a_colliding_slug(db):
    async with db.immediate() as conn:
        for task_id in ("t3", "t4", "t5"):
            await _create(db, conn, task_id, "Same Title")
        names = [
            await reserve_branch_name(conn, task_id=task_id, title="Same Title", is_epic=True)
            for task_id in ("t3", "t4", "t5")
        ]
    assert names == ["aq/epic/same-title", "aq/epic/same-title-2", "aq/epic/same-title-3"]


async def test_reserve_is_idempotent_for_the_same_task(db):
    async with db.immediate() as conn:
        await _create(db, conn, "t6", "Stable Name")
        first = await reserve_branch_name(conn, task_id="t6", title="Stable Name", is_epic=True)
        second = await reserve_branch_name(conn, task_id="t6", title="Different Name", is_epic=True)
        stored = await conn.execute(select(tasks.c.branch_name).where(tasks.c.id == "t6"))
    assert first == second == stored.scalar_one() == "aq/epic/stable-name"


async def test_reserve_allows_the_same_slug_in_different_repositories(db):
    async with db.immediate() as conn:
        await _create(db, conn, "a", "Same Title", repo_id="repo-a")
        await _create(db, conn, "b", "Same Title", repo_id="repo-b")
        first = await reserve_branch_name(conn, task_id="a", title="Same Title", is_epic=True)
        second = await reserve_branch_name(conn, task_id="b", title="Same Title", is_epic=True)
    assert first == second == "aq/epic/same-title"
