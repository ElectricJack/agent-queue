"""A finished train epic opens one pull request for its collected children."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import epic_dependencies, projects, tasks
from src.integration.epic_pr import EpicPullRequestService, render_body
from src.integration.parent_completion import ParentCompletion
from src.models import Project, RepoConfig, RepoSourceType
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db():
    database = Database(lease_dsn("epic-pr.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="epic pr project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.LINK,
            url="https://github.com/o/r.git",
            source_path="/repo/checkout",
            default_branch="main",
        )
    )
    await database.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="repo"
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e1",
                project_id="p",
                repo_id="repo",
                title="Retire the publisher",
                description="",
                status="COMPLETED",
                branch_name="aq/epic/retire-the-publisher",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        for child_id, title in (("c1", "Remove the tick"), ("c2", "Remove the sweep")):
            await conn.execute(
                insert(tasks).values(
                    id=child_id,
                    project_id="p",
                    repo_id="repo",
                    title=title,
                    description="",
                    status="COMPLETED",
                    parent_task_id="e1",
                    branch_name=f"aq/{child_id}",
                    created_at=1.0,
                    updated_at=1.0,
                )
            )
    yield database
    await database.close()


def test_body_lists_children_and_carries_the_epic_trailer():
    body = render_body(
        epic_id="e1",
        epic_title="Retire the publisher",
        children=[{"id": "c1", "title": "Remove the tick"}],
        dependencies=[],
    )
    assert "## Children" in body
    assert "- `c1` Remove the tick" in body
    assert body.rstrip().endswith("AQ-Epic: e1")


def test_body_states_declared_dependencies():
    body = render_body(
        epic_id="e2",
        epic_title="Build on it",
        children=[{"id": "c9", "title": "Do the thing"}],
        dependencies=[{"id": "e1", "title": "Retire the publisher"}],
    )
    assert "## Depends on" in body
    assert "- `e1` Retire the publisher" in body


def test_body_omits_the_dependency_section_when_there_are_none():
    body = render_body(
        epic_id="e1", epic_title="Standalone", children=[{"id": "c1", "title": "X"}], dependencies=[]
    )
    assert "## Depends on" not in body


async def test_open_creates_one_pr_and_records_the_url(db):
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/7"
    service = EpicPullRequestService(db, git_manager=git, clock=lambda: 100.0)

    result = await service.open_for_epic("e1")

    assert result == {"outcome": "opened", "epic_id": "e1", "pr_url": "https://github.com/o/r/pull/7"}
    git.bind_github_repository.assert_awaited_once_with("https://github.com/o/r.git")
    git.acreate_pr.assert_awaited_once_with(
        "/repo/checkout",
        branch="aq/epic/retire-the-publisher",
        title="Retire the publisher",
        body=render_body(
            epic_id="e1",
            epic_title="Retire the publisher",
            children=[
                {"id": "c1", "title": "Remove the tick"},
                {"id": "c2", "title": "Remove the sweep"},
            ],
            dependencies=[],
        ),
        base="main",
        project_id="p",
        repository=git.bind_github_repository.return_value,
    )
    async with db.immediate() as conn:
        stored = (await conn.execute(select(tasks.c.pr_url).where(tasks.c.id == "e1"))).scalar_one()
    assert stored == "https://github.com/o/r/pull/7"


async def test_open_is_idempotent(db):
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/7"
    service = EpicPullRequestService(db, git_manager=git)

    await service.open_for_epic("e1")
    second = await service.open_for_epic("e1")

    assert second == {
        "outcome": "already_open",
        "epic_id": "e1",
        "pr_url": "https://github.com/o/r/pull/7",
    }
    git.acreate_pr.assert_awaited_once()


async def test_open_refuses_while_a_child_is_incomplete(db):
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "c2").values(status="READY"))
    git = AsyncMock()

    result = await EpicPullRequestService(db, git_manager=git).open_for_epic("e1")

    assert result == {"outcome": "children_incomplete", "epic_id": "e1", "pending": ["c2"]}
    git.acreate_pr.assert_not_awaited()


async def test_declared_dependencies_reach_the_body(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e0", project_id="p", title="Earlier epic", description="", status="COMPLETED",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(
            insert(epic_dependencies).values(
                dependent_task_id="e1", dependency_task_id="e0", declared_at=1.0
            )
        )
    git = AsyncMock()
    git.acreate_pr.return_value = "https://github.com/o/r/pull/8"

    await EpicPullRequestService(db, git_manager=git).open_for_epic("e1")

    assert "- `e0` Earlier epic" in git.acreate_pr.await_args.kwargs["body"]


async def test_open_refuses_non_train_or_nested_parent(db):
    git = AsyncMock()
    service = EpicPullRequestService(db, git_manager=git)
    async with db.immediate() as conn:
        await conn.execute(
            insert(tasks).values(
                id="e0", project_id="p", title="Root", description="", status="COMPLETED",
                created_at=1.0, updated_at=1.0,
            )
        )
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(parent_task_id="e0"))
    assert await service.open_for_epic("e1") == {"outcome": "not_root_epic", "epic_id": "e1"}
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(parent_task_id=None))
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(hierarchical_integration_mode="hierarchy")
        )
    assert await service.open_for_epic("e1") == {"outcome": "not_train", "epic_id": "e1"}
    git.acreate_pr.assert_not_awaited()


async def test_parent_completion_retries_pr_after_github_failure(db):
    git = AsyncMock()
    git.acreate_pr.side_effect = [RuntimeError("temporary GitHub failure"), "https://github.com/o/r/pull/7"]
    completion = ParentCompletion(db, git_manager=git)
    completion._complete_parent_transition = AsyncMock(
        side_effect=[
            {"outcome": "completed", "task_id": "e1"},
            {"outcome": "already_completed", "task_id": "e1"},
            {"outcome": "already_completed", "task_id": "e1"},
        ]
    )

    first = await completion.complete_parent("e1", 1, "a" * 40)
    second = await completion.complete_parent("e1", 1, "a" * 40)
    third = await completion.complete_parent("e1", 1, "a" * 40)

    assert first == {
        "outcome": "pr_open_failed",
        "task_id": "e1",
        "reason": "temporary GitHub failure",
    }
    assert second == third == {"outcome": "already_completed", "task_id": "e1"}
    assert git.acreate_pr.await_count == 2
